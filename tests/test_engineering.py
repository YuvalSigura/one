import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from one_mcp.engineering import (WORKFLOWS, acceptance_for, accept_artifact, make_plan,
                                 queue_ready, read_plan, save_lesson, status, write_plan)
from one_mcp.store import Store

SHA = "a" * 40


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "one.db")
        self.plan = make_plan(plan_id="rdl-rb02", workflow="defect", objective="Fix strict JSON checks",
                              repository="/fixtures/rdl", base_commit=SHA, allowed_paths=["tools", "tests"])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _complete_task_with_artifact(self, stage_id, kind):
        task = next(x for x in status(self.store, self.plan) if x["stage"] == stage_id)
        self.assertEqual(task["state"], "queued")
        task_id = task["task_id"]
        assignee = task["assignee"]
        self.store.approve(task_id, "approved")
        claimed = self.store.claim_next(assignee)
        self.assertEqual(claimed["id"], task_id)
        self.store.complete(task_id, assignee, "completed", "process exit zero only")
        file = self.root / f"{stage_id}.txt"
        file.write_text("evidence from local artifact for " + stage_id, encoding="utf-8")
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        art = self.store.add_artifact(task_id, assignee, kind, str(file), digest)
        return task_id, art["id"], file

    def test_templates_have_valid_dags(self):
        self.assertGreaterEqual(len(WORKFLOWS), 7)
        for flow in WORKFLOWS:
            plan = make_plan(plan_id="foo-" + flow.replace("_", "-"), workflow=flow,
                             objective="Audit isolated project", repository="/fixture", base_commit=SHA,
                             allowed_paths=["src"])
            seen = set()
            for stage in plan["stages"]:
                self.assertTrue(set(stage["needs"]).issubset(seen))
                seen.add(stage["id"])

    def test_no_paid_run_and_dependency_gate(self):
        preview = queue_ready(self.store, self.plan)
        self.assertEqual(preview["ready"], ["reproduce"])
        self.assertFalse(self.store.list_tasks())
        queued = queue_ready(self.store, self.plan, apply=True)
        self.assertEqual(len(queued["queued"]), 1)
        self.assertEqual(queued["queued"][0]["approval"], "pending")
        self.assertIsNone(self.store.claim_next("antigravity"))
        self.assertEqual(queue_ready(self.store, self.plan, apply=True)["queued"], [])
        task_id, art_id, artifact_path = self._complete_task_with_artifact("reproduce", "reproduction_report")
        self.assertEqual(status(self.store, self.plan)[1]["missing"], ["reproduce"])
        self.assertEqual(queue_ready(self.store, self.plan)["ready"], [])
        acceptance = accept_artifact(self.store, self.plan, "reproduce", art_id, artifact_path)
        self.assertTrue(acceptance["accepted"])
        self.assertEqual(queue_ready(self.store, self.plan)["ready"], ["red-test"])
        self.assertEqual(queue_ready(self.store, self.plan, apply=True)["queued"][0]["stage"], "red-test")
        self.assertEqual(self.store.task(task_id)["status"], "completed")
        self.assertTrue(acceptance_for(self.store, task_id))

    def test_cannot_accept_unfinished_or_wrong_evidence(self):
        queue_ready(self.store, self.plan, apply=True)
        task = status(self.store, self.plan)[0]
        f = self.root / "x"; f.write_text("good")
        with self.assertRaisesRegex(ValueError, "completed"):
            accept_artifact(self.store, self.plan, "reproduce", "ART-UNKNOWN", f)
        task_id, art_id, artifact = self._complete_task_with_artifact("reproduce", "wrong_kind")
        with self.assertRaisesRegex(ValueError, "artifact id/kind"):
            accept_artifact(self.store, self.plan, "reproduce", art_id, artifact)
        correct = self.store.add_artifact(task_id, "antigravity", "reproduction_report", str(artifact), "0" * 64)
        with self.assertRaisesRegex(ValueError, "bytes do not match"):
            accept_artifact(self.store, self.plan, "reproduce", correct["id"], artifact)
        self.assertIsNone(acceptance_for(self.store, task_id))

    def test_duplicate_acceptance_idempotent_but_not_rewritable(self):
        queue_ready(self.store, self.plan, apply=True)
        task_id, art_id, file = self._complete_task_with_artifact("reproduce", "reproduction_report")
        accept_artifact(self.store, self.plan, "reproduce", art_id, file)
        self.assertTrue(accept_artifact(self.store, self.plan, "reproduce", art_id, file)["idempotent"])
        art2 = self.store.add_artifact(task_id, "antigravity", "reproduction_report", str(file), hashlib.sha256(file.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValueError, "different acceptance"):
            accept_artifact(self.store, self.plan, "reproduce", art2["id"], file)

    def test_expired_running_fails_closed_and_does_not_unblock(self):
        queue_ready(self.store, self.plan, apply=True)
        task = status(self.store, self.plan)[0]
        self.store.approve(task["task_id"], "approved")
        self.store.claim_next("antigravity", lease_seconds=10)
        self.store.recover_expired(now=10**13)
        self.assertEqual(queue_ready(self.store, self.plan)["ready"], [])
        self.assertEqual(status(self.store, self.plan)[0]["state"], "blocked")

    def test_plan_file_frozen_and_duplicate_keys_fail_closed(self):
        p = self.root / "plan.json"
        write_plan(p, self.plan)
        self.assertEqual(read_plan(p), self.plan)
        with self.assertRaises(FileExistsError):
            write_plan(p, self.plan)
        content = p.read_text(encoding="utf-8").replace('"schema":', '"schema": "one-engineering/v1", "schema":', 1)
        p.write_text(content)
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            read_plan(p)

    def test_validation_rejects_corrupt_dependency_or_role(self):
        bad = json.loads(json.dumps(self.plan))
        bad["stages"][0]["needs"] = ["review"]
        with self.assertRaisesRegex(ValueError, "template"):
            queue_ready(self.store, bad)
        bad2 = json.loads(json.dumps(self.plan))
        bad2["assignees"]["implementer"] = "not-a-worker"
        with self.assertRaisesRegex(ValueError, "non-worker"):
            queue_ready(self.store, bad2)

    def test_plan_is_immutable_after_first_queue(self):
        queue_ready(self.store, self.plan, apply=True)
        edited = json.loads(json.dumps(self.plan))
        edited["objective"] = "Silent revision of prior objective"
        with self.assertRaisesRegex(ValueError, "already bound"):
            queue_ready(self.store, edited)
        with self.assertRaisesRegex(ValueError, "already bound"):
            queue_ready(self.store, edited, apply=True)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_review_worker_must_differ_from_implementer(self):
        with self.assertRaisesRegex(ValueError, "reviewer cannot"):
            make_plan(plan_id="bad-review", workflow="defect", objective="Check review independence",
                      repository="/fixture", base_commit=SHA, allowed_paths=["src"],
                      assignees={"implementer": "codex", "reviewer": "codex"})

    def test_lessons_are_local_only_after_acceptance(self):
        queue_ready(self.store, self.plan, apply=True)
        path = self.root / "lessons.jsonl"
        with self.assertRaisesRegex(ValueError, "operator-accepted"):
            save_lesson(self.store, self.plan, "reproduce", "Good debugging technique", path)
        _, art_id, file = self._complete_task_with_artifact("reproduce", "reproduction_report")
        accept_artifact(self.store, self.plan, "reproduce", art_id, file)
        result = save_lesson(self.store, self.plan, "reproduce", "Always check the old bug first", path)
        self.assertFalse(result["automatic_promotion"])
        self.assertEqual(json.loads(path.read_text().splitlines()[0])["scope"], "project-local-unreviewed")


if __name__ == "__main__":
    unittest.main()

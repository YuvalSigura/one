"""Offline tests: exact role routing, no API/provider calls, and fail-closed review gates."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from one_mcp.store import Store
from one_mcp.roleflow import RoleFlow
from one_mcp.receipts import make_manifest

SHA = "a" * 40


def example_plan():
    def step(key, role, agent, parents=(), paths=None):
        return {"key": key, "role": role, "title": key, "goal": "Implement " + key,
                "assignee": agent, "base_commit": SHA, "allowed_paths": paths or [],
                "acceptance": ["Report tests and exact file:line evidence"],
                "depends_on": [{"step": p, "gate": gate} for p, gate in parents]}
    return {"id": "sample-flow", "description": "Handoff acceptance test",
            "project_context": "No push. Preserve all user work. Honor project invariants.",
            "steps": [step("build", "implementer", "codex", paths=["src"]),
                      step("review", "reviewer", "claude", [("build", "completed")]),
                      step("integrate", "integrator", "antigravity", [("review", "accepted")])]}


class RoleFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state.db")
        self.flow = RoleFlow(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_idempotent_frozen_plan_and_no_default_approval(self):
        first = self.flow.create(example_plan())
        self.assertEqual(first, self.flow.create(example_plan()))
        self.assertEqual(len(self.store.list_tasks()), 3)
        self.assertTrue(first["steps"][0]["ready"])
        self.assertEqual(self.flow.ready("sample-flow"), [])
        alt = example_plan()
        alt["steps"][0]["goal"] = "different goal"
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.flow.create(alt)

    def test_cycles_missing_deps_and_bad_roles_leave_no_partial_tasks(self):
        for modifier in (
            lambda p: p["steps"][0].update(depends_on=[{"step": "integrate", "gate": "completed"}]),
            lambda p: p["steps"][0].update(depends_on=[{"step": "unknown", "gate": "completed"}]),
            lambda p: p["steps"][0].update(role="unknown"),
            lambda p: p["steps"][0].update(base_commit="HEAD"),
            lambda p: p["steps"][1].update(allowed_paths=["../secrets"]),
        ):
            p = example_plan()
            modifier(p)
            with self.assertRaises(ValueError):
                self.flow.create(p)
            self.assertEqual(self.store.list_tasks(), [])

    def test_atomic_claims_are_dependency_gated_even_after_bulk_approval(self):
        stages = self.flow.create(example_plan())["steps"]
        self.flow.approve_all("sample-flow")
        self.assertEqual([s["key"] for s in self.flow.ready("sample-flow")], ["build"])
        self.assertIsNone(self.store.claim_next("claude"))
        self.assertIsNone(self.store.claim_next("antigravity"))
        build, review, integrate = [s["task_id"] for s in stages]
        self.assertEqual(self.store.claim_next("codex")["id"], build)
        self.assertIsNone(self.store.claim_next("claude"))
        self.store.complete(build, "codex", "completed", "patch")
        self.assertEqual(self.store.claim_next("claude")["id"], review)
        self.store.complete(review, "claude", "completed", "exit 0 is not proof")
        self.assertIsNone(self.store.claim_next("antigravity"))
        self.flow.verdict(review, "accepted", "operator checked independent review evidence")
        self.assertEqual(self.store.claim_next("antigravity")["id"], integrate)

    def test_rejected_review_is_permanent_gate_not_verified(self):
        stages = self.flow.create(example_plan())["steps"]
        self.flow.approve_all("sample-flow")
        build, review = [s["task_id"] for s in stages[:2]]
        self.store.claim_next("codex")
        self.store.complete(build, "codex", "completed", "patch")
        self.store.claim_next("claude")
        self.store.complete(review, "claude", "completed", "found P1")
        decision = self.flow.verdict(review, "rejected", "P1 at src/test.py:42")
        self.assertFalse(decision["verified"])
        self.assertIsNone(self.store.claim_next("antigravity"))
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.flow.verdict(review, "accepted", "override")

    def test_failed_or_blocked_work_never_triggers_downstream(self):
        tasks = self.flow.create(example_plan())["steps"]
        self.flow.approve_all("sample-flow")
        self.store.claim_next("codex")
        self.store.complete(tasks[0]["task_id"], "codex", "blocked", "provider quota exhausted")
        self.assertFalse(self.flow.ready("sample-flow"))
        self.assertIsNone(self.store.claim_next("claude"))

    def test_precise_handoff_and_tampered_receipt_fails_closed(self):
        stages = self.flow.create(example_plan())["steps"]
        build, review = stages[0]["task_id"], stages[1]["task_id"]
        self.store.approve(build, "approved")
        self.store.claim_next("codex")
        root = self.root / "artifacts"
        target = root / build / "changes.json"
        target.parent.mkdir(parents=True)
        manifest = {"changed_paths": ["src/a.py"], "outside_allowlist": [],
                    "line_hints": [{"path": "src/a.py", "lines": ["L10-L18"]}],
                    "handoff_summary": {"summary": "Changed only authorization validation"}}
        prior_workspace = self.root / "prior-worktree"
        (prior_workspace / "src").mkdir(parents=True)
        source = prior_workspace / "src" / "a.py"
        source.write_text("\n".join(f"x{i} = {i}" for i in range(1, 25)) + "\n", encoding="utf-8")
        manifest["file_sha256"] = {"src/a.py": hashlib.sha256(source.read_bytes()).hexdigest()}
        target.write_text(json.dumps(manifest), encoding="utf-8")
        self.store.add_artifact(build, "codex", "change_manifest", str(target),
                                hashlib.sha256(target.read_bytes()).hexdigest())
        self.store.complete(build, "codex", "completed", "exited 0",
                            workspace=str(prior_workspace))
        packet = self.flow.handoff(review, root)
        self.assertIn("L10-L18", packet)
        self.assertIn("src/a.py", packet)
        self.assertIn("No push", packet)
        self.assertIn("read-only", packet.lower())
        self.assertIn("UNTRUSTED source excerpt", packet)
        self.assertIn("L10: x10 = 10", packet)
        self.assertLessEqual(len(packet), 5501)
        target.write_text("TAMPERED", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "untrusted or stale"):
            self.flow.handoff(review, root)

    def test_model_stdout_handoff_is_bounded_and_untrusted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "fix.py").write_text("x = 1\n")
            stdout = root / "stdout.log"
            stdout.write_text("noise\nONE_HANDOFF_JSON: " + json.dumps(
                {"summary": "test pass", "tests": ["pytest -q: pass"],
                 "risks": ["none"], "next_focus": ["src/fix.py:1"]}) + "\n")
            with patch("one_mcp.receipts._diff_hunks", return_value={}):
                receipt = make_manifest(root, ["src/fix.py"], [], stdout)
            self.assertEqual(receipt["line_hints"][0]["path"], "src/fix.py")
            self.assertEqual(receipt["handoff_summary"]["summary"], "test pass")
            self.assertIn("src/fix.py", receipt["file_sha256"])

    def test_dry_run_and_disabled_workers_do_not_launch(self):
        self.flow.create(example_plan())
        cfg = self.root / "agents.json"
        cfg.write_text(json.dumps({"agents": {"codex": {"enabled": False}}}))
        self.assertTrue(self.flow.execute_approved("sample-flow", cfg)["dry_run"])
        result = self.flow.execute_approved("sample-flow", cfg, execute=True)
        self.assertEqual(result["executions"], [])


    def test_flow_cannot_attach_preseeded_tasks(self):
        plan = example_plan()
        build = plan["steps"][0]
        self.store.create_task(build["title"], build["goal"], build["assignee"],
                               owner="operator", base_commit=build["base_commit"],
                               allowed_paths=build["allowed_paths"],
                               acceptance=build["acceptance"], request_key="flow:sample-flow:build")
        with self.assertRaisesRegex(ValueError, "preexisting task key"):
            self.flow.create(plan)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_local_plan_rejects_duplicate_json_keys(self):
        from one_mcp.roleflow import read_plan
        path = self.root / "flow.json"
        path.write_text('{"id":"sample-flow","id":"other","steps":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            read_plan(path)

    def test_automatic_implementer_to_reviewer_handoff_offline(self):
        import subprocess
        repo = self.root / "fixture"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init")
        (repo / "README.md").write_text("baseline", encoding="utf-8")
        git("add", "README.md")
        git("-c", "user.name=ONE", "-c", "user.email=one@example.test", "commit", "-m", "baseline")
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        p = example_plan()
        p["id"] = "smoke-chain"
        p["steps"] = p["steps"][:2]
        for step in p["steps"]:
            step["base_commit"] = commit
        self.flow.create(p)
        self.flow.approve_all("smoke-chain")
        wt = self.root / "worktrees"; wt.mkdir()
        artifact = self.root / "artifacts"; artifact.mkdir()
        config = self.root / "config.json"
        config.write_text(json.dumps({
            "repository": str(repo), "worktree_root": str(wt),
            "artifact_root": str(artifact),
            "agents": {
                "codex": {"enabled": True, "argv": ["codex", "exec", "-"],
                          "input_mode": "stdin", "timeout_seconds": 60},
                "claude": {"enabled": True, "argv": ["claude", "-p"],
                           "input_mode": "stdin", "timeout_seconds": 60}
            }
        }), encoding="utf-8")
        observed = []
        def fake_call(argv, cwd, stdin, timeout, stdout_path, stderr_path):
            brief = Path(stdin.decode("utf-8").splitlines()[0].split("at: ", 1)[1])
            payload = brief.read_text(encoding="utf-8")
            if argv[0] == "codex":
                (cwd / "src").mkdir()
                (cwd / "src" / "change.py").write_text("changed = True\n")
                self.assertIn("implementer", payload)
                stdout_path.write_text('ONE_HANDOFF_JSON: {"summary":"changed module","next_focus":["src/change.py:1"]}\n')
            else:
                self.assertIn("src/change.py", payload)
                self.assertIn("changed module", payload)
                self.assertIn("L1-L1", payload)
                self.assertIn("reviewer", payload)
                observed.append(payload)
                stdout_path.write_text('ONE_HANDOFF_JSON: {"summary":"review delivered"}\n')
            stderr_path.write_text("")
            return 0, False
        with patch("one_mcp.worker._capture", side_effect=fake_call):
            result = self.flow.execute_approved("smoke-chain", config,
                                               max_tasks=2, parallel=2, execute=True)
        self.assertEqual(len(result["executions"]), 2)
        self.assertEqual([v["status"] for v in result["executions"]], ["completed", "completed"])
        self.assertTrue(observed)
        self.assertEqual([s["status"] for s in self.flow.status("smoke-chain")["steps"]],
                         ["completed", "completed"])
        self.assertEqual((repo / "README.md").read_text(), "baseline")


if __name__ == "__main__":
    unittest.main()

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
        self.flow.activate_ready("sample-flow")
        self.assertEqual(self.store.claim_next("claude")["id"], review)
        self.store.complete(review, "claude", "completed", "exit 0 is not proof")
        self.assertIsNone(self.store.claim_next("antigravity"))
        self.flow.verdict(review, "accepted", "operator checked independent review evidence")
        self.flow.activate_ready("sample-flow")
        self.assertEqual(self.store.claim_next("antigravity")["id"], integrate)

    def test_rejected_review_is_permanent_gate_not_verified(self):
        stages = self.flow.create(example_plan())["steps"]
        self.flow.approve_all("sample-flow")
        build, review = [s["task_id"] for s in stages[:2]]
        self.store.claim_next("codex")
        self.store.complete(build, "codex", "completed", "patch")
        self.flow.activate_ready("sample-flow")
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

    def test_transitive_context_survives_reviewer_without_code_changes(self):
        steps = self.flow.create(example_plan())["steps"]
        self.flow.approve_all("sample-flow")
        build, review, integrate = [step["task_id"] for step in steps]
        artifact = self.root / "artifacts"
        implementation = self.root / "implementer-worktree"
        (implementation / "src").mkdir(parents=True)
        code = implementation / "src" / "change.py"
        code.write_text("old = False\nnew = True\n")
        self.store.claim_next("codex")
        manifest = {
            "changed_paths": ["src/change.py"], "outside_allowlist": [],
            "line_hints": [{"path": "src/change.py", "lines": ["L2-L2"]}],
            "file_sha256": {"src/change.py": hashlib.sha256(code.read_bytes()).hexdigest()},
            "handoff_summary": {"summary": "implementation summary"}
        }
        def register(task_id, worker, doc):
            path = artifact / task_id / "changes.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc))
            self.store.add_artifact(task_id, worker, "change_manifest", str(path),
                                    hashlib.sha256(path.read_bytes()).hexdigest())
        register(build, "codex", manifest)
        self.store.complete(build, "codex", "completed", "source changed", workspace=str(implementation))
        self.flow.activate_ready("sample-flow")
        self.store.claim_next("claude")
        reviewer_worktree = self.root / "reviewer-worktree"
        reviewer_worktree.mkdir()
        register(review, "claude", {
            "changed_paths": [], "outside_allowlist": [], "line_hints": [],
            "file_sha256": {}, "handoff_summary": {"summary": "reviewed implementation"}})
        self.store.complete(review, "claude", "completed", "review ready",
                            workspace=str(reviewer_worktree))
        self.flow.verdict(review, "accepted", "local operator reviewed exact diff")
        message = self.flow.handoff(integrate, artifact)
        self.assertIn("src/change.py", message)
        self.assertIn("L2: new = True", message)
        self.assertIn("reviewed implementation", message)
        self.assertIn("implementation summary", message)
        self.assertLessEqual(len(message), 5501)
        # Stale ancestor snapshots must fail closed even if direct review is intact.
        code.write_text("tampered = True\n")
        with self.assertRaisesRegex(ValueError, "untrusted or stale"):
            self.flow.handoff(integrate, artifact)

    def test_reviewer_cannot_approve_its_own_implementation(self):
        plan = example_plan()
        plan["steps"][1]["assignee"] = "codex"
        with self.assertRaisesRegex(ValueError, "same worker"):
            self.flow.create(plan)
        self.assertEqual(self.store.list_tasks(), [])

    def test_project_context_secrets_redacted_before_storage(self):
        plan = example_plan()
        plan["project_context"] = "No push; api_key=NEVER_WRITE_RAW_VALUE"
        plan["steps"][0]["context"] = "Password: local-secret-value"
        self.flow.create(plan)
        captured = self.store.db.execute("SELECT project_context FROM flow_plans WHERE id=?",
                                         ("sample-flow",)).fetchone()[0]
        self.assertNotIn("NEVER_WRITE_RAW_VALUE", captured)
        context = self.store.db.execute("SELECT context FROM flow_steps WHERE flow_id=? AND step_key=?",
                                        ("sample-flow", "build")).fetchone()[0]
        self.assertNotIn("local-secret-value", context)

    def test_no_rediscovery_policy_and_essential_decisions_survive_handoff(self):
        plan = example_plan()
        plan["steps"][1]["handoff_mode"] = "execution_only"
        plan["steps"][1]["must_know"] = {
            "decisions": ["Architectural F1-F12 contracts are frozen"],
            "do_not_repeat": ["Prior root-cause investigation already completed"]}
        plan["steps"][1]["required_handoff"] = ["changed_files", "line_hints", "summary"]
        tasks = self.flow.create(plan)["steps"]
        prior, following = tasks[0]["task_id"], tasks[1]["task_id"]
        artifact = self.root / "artifacts"
        worktree = self.root / "upstream"
        (worktree / "src").mkdir(parents=True)
        codefile = worktree / "src" / "fix.py"
        codefile.write_text("fix = True\n")
        self.store.approve(prior, "approved")
        self.store.claim_next("codex")
        manifest = {
            "changed_paths": ["src/fix.py"], "outside_allowlist": [],
            "line_hints": [{"path": "src/fix.py", "lines": ["L1-L1"]}],
            "file_sha256": {"src/fix.py": hashlib.sha256(codefile.read_bytes()).hexdigest()},
            "handoff_summary": {"summary": "Fixed root cause", "tests": ["unit tests passed"]}
        }
        receipt = artifact / prior / "changes.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(manifest))
        self.store.add_artifact(prior, "codex", "change_manifest", str(receipt),
                                hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.store.complete(prior, "codex", "completed", "finished", workspace=str(worktree))
        prompt = self.flow.handoff(following, artifact)
        self.assertIn("DO NOT restart discovery", prompt)
        self.assertIn("Prior root-cause investigation already completed", prompt)
        self.assertIn("F1-F12 contracts are frozen", prompt)
        self.assertIn("L1-L1", prompt)
        self.assertIn("Required context fields present", prompt)
        self.assertLessEqual(len(prompt), 5500)

    def test_missing_required_evidence_blocks_next_agent_before_launch(self):
        plan = example_plan()
        plan["steps"][1]["required_handoff"] = ["tests"]
        tasks = self.flow.create(plan)["steps"]
        prior, following = tasks[0]["task_id"], tasks[1]["task_id"]
        root = self.root / "artifacts"
        predecessor_worktree = self.root / "empty-predecessor"
        predecessor_worktree.mkdir()
        receipt = root / prior / "changes.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "changed_paths": [], "outside_allowlist": [], "line_hints": [],
            "file_sha256": {}, "handoff_summary": {"summary": "No test record"}
        }))
        self.store.approve(prior, "approved")
        self.store.claim_next("codex")
        self.store.add_artifact(prior, "codex", "change_manifest", str(receipt),
                                hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.store.complete(prior, "codex", "completed", "finished",
                            workspace=str(predecessor_worktree))
        with self.assertRaisesRegex(ValueError, "ONE_CONTEXT_GAP.*tests"):
            self.flow.handoff(following, root)

    def test_essential_context_never_silently_truncated(self):
        plan = example_plan()
        plan["project_context"] = "B" * 2300
        plan["steps"][0]["context"] = "C" * 2300
        plan["steps"][0]["must_know"] = {"decisions": ["D" * 250] * 5}
        stage = self.flow.create(plan)["steps"][0]["task_id"]
        artifact = self.root / "artifacts"
        artifact.mkdir()
        with self.assertRaisesRegex(ValueError, "ONE_CONTEXT_GAP.*budget"):
            self.flow.handoff(stage, artifact)

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

    def test_plan_preapproval_only_unlocks_ready_tasks_for_old_workers(self):
        self.flow.create(example_plan())
        approved = self.flow.approve_all("sample-flow")
        self.assertTrue(approved["operator_authorized"])
        self.assertEqual([x["approval"] for x in approved["steps"]],
                         ["approved", "pending", "pending"])
        # Simulate the legacy ONE v0.2 worker selection query. Future tasks
        # are invisible even though the operator approved the entire plan.
        for agent in ("claude", "antigravity"):
            found = self.store.db.execute(
                "SELECT id FROM tasks WHERE status='queued' AND approval='approved' AND assignee=?",
                (agent,)).fetchone()
            self.assertIsNone(found)
        self.assertEqual([x["key"] for x in self.flow.ready("sample-flow")], ["build"])
        self.assertEqual(len(self.store.list_tasks()), 3)

    def test_revoke_stops_future_approved_tasks_and_cannot_be_reauthorized(self):
        self.flow.create(example_plan())
        self.flow.approve_all("sample-flow")
        outcome = self.flow.revoke("sample-flow")
        self.assertFalse(outcome["operator_authorized"])
        self.assertEqual(outcome["steps"][0]["approval"], "rejected")
        self.assertEqual(self.flow.ready("sample-flow"), [])
        self.assertIsNone(self.store.claim_next("codex"))
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.flow.activate_ready("sample-flow")
        with self.assertRaisesRegex(ValueError, "revoked"):
            self.flow.approve_all("sample-flow")

    def test_dry_run_and_disabled_workers_do_not_launch(self):
        self.flow.create(example_plan())
        cfg = self.root / "agents.json"
        cfg.write_text(json.dumps({"agents": {"codex": {"enabled": False}}}))
        self.assertTrue(self.flow.execute_approved("sample-flow", cfg)["dry_run"])
        with self.assertRaisesRegex(ValueError, "authorization"):
            self.flow.execute_approved("sample-flow", cfg, execute=True)
        self.flow.approve_all("sample-flow")
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

    def test_parallel_independent_workers_claim_separate_tasks_once(self):
        import subprocess
        import threading
        repo = self.root / "parallel-fixture"
        repo.mkdir()
        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init")
        (repo / "README.md").write_text("baseline")
        git("add", "README.md")
        git("-c", "user.name=ONE", "-c", "user.email=one@example.test", "commit", "-m", "init")
        commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        plan = example_plan()
        plan["id"] = "parallel-smoke"
        plan["steps"] = plan["steps"][:2]
        plan["steps"][1]["role"] = "tester"
        plan["steps"][1]["depends_on"] = []
        for step in plan["steps"]:
            step["base_commit"] = commit
        self.flow.create(plan)
        self.flow.approve_all("parallel-smoke")
        worktrees = self.root / "parallel-worktrees"; worktrees.mkdir()
        artifacts = self.root / "parallel-artifacts"; artifacts.mkdir()
        config = self.root / "parallel.json"
        config.write_text(json.dumps({
            "repository": str(repo), "worktree_root": str(worktrees),
            "artifact_root": str(artifacts),
            "agents": {
                "codex": {"enabled": True, "argv": ["codex", "exec", "-"],
                          "input_mode": "stdin", "timeout_seconds": 60},
                "claude": {"enabled": True, "argv": ["claude", "-p"],
                           "input_mode": "stdin", "timeout_seconds": 60}
            }
        }))
        barrier = threading.Barrier(2)
        invocations = []
        guard = threading.Lock()
        def fake_capture(argv, cwd, stdin, timeout, stdout_path, stderr_path):
            with guard:
                invocations.append(argv[0])
            barrier.wait(timeout=15)
            stdout_path.write_text('ONE_HANDOFF_JSON: {"summary":"parallel stage complete"}\n')
            stderr_path.write_text("")
            return 0, False
        with patch("one_mcp.worker._capture", side_effect=fake_capture):
            result = self.flow.execute_approved("parallel-smoke", config, execute=True,
                                                max_tasks=2, parallel=2)
        self.assertCountEqual(invocations, ["codex", "claude"])
        self.assertEqual(len(result["executions"]), 2)
        self.assertTrue(all(x["status"] == "completed" for x in result["executions"]))

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

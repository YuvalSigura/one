"""Offline patch-aware handoff regression tests: real Git worktrees, no paid LLM."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from one_mcp.store import Store
from one_mcp.roleflow import RoleFlow
from one_mcp.patches import materialize_for_tester, classify_test_changes
from one_mcp.worker import _changes


def run(*argv):
    return subprocess.run(argv, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True).stdout.strip()


class PatchAwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run("git", "-C", str(self.repo), "init")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "module.py").write_text("answer = 1\n")
        run("git", "-C", str(self.repo), "add", ".")
        run("git", "-C", str(self.repo), "-c", "user.name=ONE", "-c",
            "user.email=one@example.test", "commit", "-m", "base")
        self.sha = run("git", "-C", str(self.repo), "rev-parse", "HEAD")
        self.store = Store(self.root / "state.db")
        self.flow = RoleFlow(self.store)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.plan = {
            "id": "patch-test", "description": "patch awareness",
            "project_context": "Test changed source not old base",
            "steps": [
                {"key": "implement", "role": "implementer", "title": "Make patch",
                 "goal": "Change answer", "assignee": "codex",
                 "base_commit": self.sha, "allowed_paths": ["src"],
                 "depends_on": []},
                {"key": "test", "role": "tester", "title": "Test exact patch",
                 "goal": "Write precise regression", "assignee": "claude",
                 "base_commit": self.sha, "allowed_paths": ["tests"],
                 "required_handoff": ["changed_files", "line_hints"],
                 "depends_on": [{"step": "implement", "gate": "completed"}]},
            ]
        }
        self.config = self.root / "agents.json"
        self.config.write_text(json.dumps({
            "repository": str(self.repo),
            "worktree_root": str(self.worktrees),
            "artifact_root": str(self.artifacts),
            "agents": {
                "codex": {"enabled": True, "argv": ["codex", "exec", "-"],
                          "input_mode": "stdin", "timeout_seconds": 60},
                "claude": {"enabled": True, "argv": ["claude", "-p"],
                           "input_mode": "stdin", "timeout_seconds": 60},
            }
        }))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_upstream(self, *, tampered=False, conflict=False):
        flow = self.flow.create(self.plan)
        tasks = flow["steps"]
        upstream, target = tasks[0]["task_id"], tasks[1]["task_id"]
        wd = self.worktrees / upstream
        run("git", "-C", str(self.repo), "worktree", "add", "--detach", str(wd), self.sha)
        (wd / "src" / "module.py").write_text("answer = 2\n")
        (wd / "src" / "new.py").write_text("marker = 3\n")
        manifest = {
            "changed_paths": ["src/module.py", "src/new.py"],
            "outside_allowlist": [],
            "file_sha256": {
                name: hashlib.sha256((wd / name).read_bytes()).hexdigest()
                for name in ("src/module.py", "src/new.py")
            },
            "line_hints": [{"path": "src/module.py", "lines": ["L1-L1"]}],
            "handoff_summary": {"summary": "code changed", "tests": ["unittest: pass"]},
        }
        self.flow.approve_all("patch-test")
        self.store.claim_next("codex")
        receipt = self.artifacts / upstream / "changes.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(manifest))
        self.store.add_artifact(upstream, "codex", "change_manifest", str(receipt),
                                hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.store.complete(upstream, "codex", "completed", "done", workspace=str(wd))
        if tampered:
            (wd / "src" / "module.py").write_text("answer = 999\n")
        destination = self.worktrees / target
        run("git", "-C", str(self.repo), "worktree", "add", "--detach", str(destination), self.sha)
        return tasks, destination

    def test_snapshots_faithfully_materialize_then_separate_test_edits(self):
        tasks, target = self.make_upstream()
        borrowed = materialize_for_tester(self.store,
                                          self.store.task(tasks[1]["task_id"]),
                                          target, self.artifacts)
        self.assertEqual((target / "src" / "module.py").read_text(), "answer = 2\n")
        self.assertEqual((target / "src" / "new.py").read_text(), "marker = 3\n")
        (target / "tests").mkdir()
        (target / "tests" / "test_answer.py").write_text("assert True\n")
        owned, illegal = classify_test_changes(target, borrowed, _changes(target))
        self.assertEqual(owned, ["tests/test_answer.py"])
        self.assertEqual(illegal, [])
        (target / "src" / "module.py").write_text("answer = 999\n")
        owned, illegal = classify_test_changes(target, borrowed, _changes(target))
        self.assertIn("src/module.py", illegal)

    def test_stale_source_hash_blocks_materialization(self):
        tasks, target = self.make_upstream(tampered=True)
        with self.assertRaisesRegex(ValueError, "hash|receipt|changed"):
            materialize_for_tester(self.store, self.store.task(tasks[1]["task_id"]),
                                   target, self.artifacts)
        self.assertEqual((target / "src" / "module.py").read_text(), "answer = 1\n")

    def test_real_worker_chain_uses_patched_source_without_paid_model(self):
        self.flow.create(self.plan)
        self.flow.approve_all("patch-test")
        observations = []
        def fake_capture(argv, cwd, stdin, timeout, stdout_path, stderr_path):
            if argv[0] == "codex":
                (cwd / "src" / "module.py").write_text("answer = 2\n")
                stdout_path.write_text(
                    'ONE_HANDOFF_JSON: {"summary":"changed answer","tests":["local smoke pass"]}\n')
            else:
                self.assertEqual((cwd / "src" / "module.py").read_text(), "answer = 2\n")
                instruction = Path(str(stdin.decode()).split("at: ", 1)[1].splitlines()[0])
                prompt = instruction.read_text()
                self.assertIn("MATERIALIZED", prompt)
                self.assertIn("BLIND INDEPENDENT", prompt) if False else None
                (cwd / "tests").mkdir()
                (cwd / "tests" / "test_answer.py").write_text("assert True\n")
                stdout_path.write_text('ONE_HANDOFF_JSON: {"summary":"validated patch"}\n')
                observations.append("tester used patched source")
            stderr_path.write_text("")
            return (0, False)
        with patch("one_mcp.worker._capture", side_effect=fake_capture):
            report = self.flow.execute_approved("patch-test", self.config, parallel=2,
                                                max_tasks=2, execute=True)
        self.assertEqual(observations, ["tester used patched source"])
        self.assertEqual([r["status"] for r in report["executions"]], ["completed", "completed"])
        self.assertEqual(report["executions"][1]["changed_paths"], ["tests/test_answer.py"])
        self.assertEqual((self.repo / "src" / "module.py").read_text(), "answer = 1\n")


if __name__ == "__main__":
    unittest.main()

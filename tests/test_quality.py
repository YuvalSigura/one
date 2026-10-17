"""Independent deterministic quality-gate checks; no provider calls."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from one_mcp.store import Store
from one_mcp.roleflow import RoleFlow
from one_mcp.quality import run_quality, assert_quality


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "code.py").write_text("answer = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=ONE", "-c", "user.email=one@example.test",
            "commit", "-m", "base")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.store = Store(self.root / "state.db")
        self.flow = RoleFlow(self.store)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"agents": {},
            "repository": str(self.repo),
            "worktree_root": str(self.worktrees),
            "artifact_root": str(self.artifacts)}))
        steps = self.flow.create({"id": "gated", "description": "quality gating",
            "steps": [{"key": "implementation", "role": "implementer",
                       "title": "Improve code", "goal": "Add change",
                       "base_commit": self.sha, "assignee": "codex",
                       "allowed_paths": ["src"],
                       "required_checks": ["diff"], "depends_on": []}]})["steps"]
        self.task_id = steps[0]["task_id"]
        self.workspace = self.worktrees / self.task_id
        git(self.repo, "worktree", "add", "--detach", str(self.workspace), self.sha)
        self.flow.approve_all("gated")
        self.store.claim_next("codex")
        (self.workspace / "src" / "code.py").write_text("answer = 2\n")
        self.store.complete(self.task_id, "codex", "completed", "ready",
                            workspace=str(self.workspace))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_operator_acceptance_requires_passing_deterministic_check(self):
        with self.assertRaisesRegex(ValueError, "quality receipt"):
            self.flow.verdict(self.task_id, "accepted", "looks correct")
        report = run_quality(self.store, self.task_id, self.config, ["diff"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["checks"]["diff"]["exit_code"], 0)
        decision = self.flow.verdict(self.task_id, "accepted", "reviewed passing diff check")
        self.assertEqual(decision["decision"], "accepted")
        self.assertFalse(decision["verified"])

    def test_tampering_after_check_fails_closed(self):
        run_quality(self.store, self.task_id, self.config, ["diff"])
        (self.workspace / "src" / "code.py").write_text("answer = 999\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.flow.verdict(self.task_id, "accepted", "untrusted old result")

    def test_malicious_or_duplicate_commands_are_never_launched(self):
        for checks in (["diff", "diff"], ["curl http://example.test"], ["powershell"]):
            with self.assertRaises(ValueError):
                run_quality(self.store, self.task_id, self.config, checks)
        self.assertFalse(any(a["kind"] == "engineering_quality"
                             for a in self.store.artifacts(self.task_id)))

    def test_failed_diff_cannot_pass_accepted_gate(self):
        (self.workspace / "src" / "code.py").write_text("answer = 2  \n")
        report = run_quality(self.store, self.task_id, self.config, ["diff"])
        self.assertFalse(report["passed"])
        with self.assertRaisesRegex(ValueError, "not passed"):
            self.flow.verdict(self.task_id, "accepted", "ignored failing git diff")
        rejected = self.flow.verdict(self.task_id, "rejected", "whitespace error in src/code.py:1")
        self.assertEqual(rejected["decision"], "rejected")


if __name__ == "__main__":
    unittest.main()

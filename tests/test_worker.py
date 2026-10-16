import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from one_mcp.store import Store
from one_mcp.context import task_brief
from one_mcp.worker import execute_one, validated_command, doctor
from one_mcp.worktrees import isolate


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        def git(*args):
            subprocess.run(["git", "-C", str(self.repo), *args],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git("init")
        (self.repo / "README.md").write_text("baseline", encoding="utf-8")
        git("add", "README.md")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=ONE",
                        "-c", "user.email=one@example.test", "commit", "-m", "init"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                     check=True, capture_output=True, text=True).stdout.strip()
        self.worktrees = self.base / "trees"
        self.worktrees.mkdir()
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()
        self.config = self.base / "config.local.json"
        self.config.write_text(json.dumps({
            "repository": str(self.repo), "worktree_root": str(self.worktrees),
            "artifact_root": str(self.artifacts),
            "agents": {"codex": {"enabled": True, "argv": ["codex", "exec", "-"],
                                  "input_mode": "stdin", "timeout_seconds": 60}}
        }), encoding="utf-8")
        self.store = Store(self.base / "one.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, allowed_paths=None):
        return self.store.create_task("P0-1", "Fix scientific signals", "codex",
                                      base_commit=self.commit,
                                      allowed_paths=allowed_paths or ["src"],
                                      acceptance=["test false positives"])

    def test_no_invocation_without_operator_approval(self):
        task = self.create()
        self.assertEqual(execute_one(self.store, "codex", self.config, dry_run=True)["status"], "dry_run")
        with patch("one_mcp.worker._capture", side_effect=AssertionError("must not execute")):
            self.assertEqual(execute_one(self.store, "codex", self.config, dry_run=False)["status"], "idle")
        self.assertFalse(list(self.worktrees.iterdir()))

    def test_isolated_worktree_and_logged_completion(self):
        task = self.create(allowed_paths=["src"])
        self.store.approve(task["id"], "approved")
        def fake_capture(argv, cwd, stdin, timeout, stdout_path, stderr_path):
            self.assertEqual(argv, ["codex", "exec", "-"])
            self.assertIn(b"Read the approved ONE development task", stdin)
            (cwd / "src").mkdir()
            (cwd / "src" / "fix.py").write_text("print('fixed')", encoding="utf-8")
            stdout_path.write_text("done", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return 0, False
        with patch("one_mcp.worker._capture", side_effect=fake_capture):
            result = execute_one(self.store, "codex", self.config, dry_run=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["changed_paths"], ["src/fix.py"])
        self.assertEqual((self.repo / "README.md").read_text(), "baseline")
        self.assertFalse((self.repo / "src").exists())
        self.assertEqual(len(self.store.artifacts(task["id"])), 3)
        self.assertEqual(self.store.task(task["id"])["status"], "completed")
        self.assertTrue(result["review_required"])

    def test_changes_outside_allowlist_are_quarantined(self):
        task = self.create(allowed_paths=["src"])
        self.store.approve(task["id"], "approved")
        def fake_capture(argv, cwd, stdin, timeout, stdout_path, stderr_path):
            (cwd / "README.md").write_text("changed", encoding="utf-8")
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            return 0, False
        with patch("one_mcp.worker._capture", side_effect=fake_capture):
            result = execute_one(self.store, "codex", self.config, dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["outside_allowlist"], ["README.md"])
        self.assertEqual((self.repo / "README.md").read_text(), "baseline")

    def test_dangerous_cli_flags_rejected(self):
        task = self.create()
        brief = self.base / "brief.md"
        for argv in (["claude", "--dangerously-skip-permissions"],
                     ["grok", "--yolo", "{brief}"],
                     ["bash", "-c", "whoami"]):
            with self.assertRaises(ValueError):
                validated_command("claude" if argv[0] == "claude" or argv[0] == "bash" else "grok-cli",
                                  {"enabled": True, "argv": argv, "input_mode": "brief_argument",
                                   "timeout_seconds": 60}, brief)

    def test_antigravity_agy_headless_command(self):
        brief = self.base / "brief.md"
        row = {"enabled": True, "input_mode": "brief_argument", "timeout_seconds": 60,
               "argv": ["C:/Users/u/AppData/Local/agy/bin/agy.exe", "--model", "gemini-4-argon-high",
                        "--mode", "accept-edits", "-p", "{brief}"]}
        argv, stdin, timeout = validated_command("antigravity", row, brief)
        self.assertIsNone(stdin)
        self.assertEqual(argv[:6], row["argv"][:6])
        self.assertIn("Read the approved ONE development task", argv[6])
        self.assertIn("antigravity", __import__("one_mcp.store", fromlist=["AGENTS"]).AGENTS)
        for bad in (["agy", "--dangerously-skip-permissions", "-p", "{brief}"],
                    ["claude.exe", "-p", "{brief}"]):
            with self.assertRaises(ValueError):
                validated_command("antigravity", dict(row, argv=bad), brief)
        task = self.store.create_task("agy", "ping", "antigravity", base_commit=self.commit,
                                      allowed_paths=["src"])
        self.assertEqual(task["assignee"], "antigravity")

    def test_unpinned_commit_fails_closed(self):
        task = self.store.create_task("unpinned", "should not write", "codex", allowed_paths=["src"])
        self.store.approve(task["id"], "approved")
        with self.assertRaises(RuntimeError):
            execute_one(self.store, "codex", self.config, dry_run=False)
        self.assertEqual(self.store.task(task["id"])["status"], "blocked")
        self.assertFalse(list(self.worktrees.iterdir()))


    @unittest.skipIf(__import__("os").name == "nt", "POSIX fake CLI smoke uses executable shebang")
    def test_real_fake_cli_end_to_end_without_provider_charge(self):
        import os
        tool_bin = self.base / "fake-bin"
        tool_bin.mkdir()
        executable = tool_bin / "codex"
        executable.write_text("#!/usr/bin/env python3\n"
                              "from pathlib import Path\n"
                              "import sys\n"
                              "assert 'Read the approved ONE' in sys.stdin.read()\n"
                              "Path('src').mkdir()\n"
                              "Path('src/receipt.py').write_text('ok=1\\n')\n"
                              "print('FAKE CODEX OFFLINE PASS')\n", encoding="utf-8")
        executable.chmod(0o755)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["agents"]["codex"]["argv"] = [str(executable), "exec", "-"]
        self.config.write_text(json.dumps(config), encoding="utf-8")
        task = self.create(allowed_paths=["src"])
        self.store.approve(task["id"], "approved")
        result = execute_one(self.store, "codex", self.config, dry_run=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["changed_paths"], ["src/receipt.py"])
        self.assertEqual(len(self.store.inbox("grok-bot")), 1)
        self.assertEqual(self.store.task(task["id"])["attempt"], 1)
        self.assertIsNone(self.store.claim_next("codex"))

    def test_context_is_bounded_and_pinned(self):
        task = self.create()
        brief = task_brief(task)
        self.assertIn(self.commit, brief)
        self.assertIn("Do not merge, push", brief)
        self.assertNotIn(".runtime/engagements/", brief)


if __name__ == "__main__":
    unittest.main()

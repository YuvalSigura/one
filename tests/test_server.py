import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from one_mcp.server import handle, serve, TOOLS
from one_mcp.store import Store


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "one.db"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def request(self, name, args, reqid=1):
        return handle(self.store, {"jsonrpc": "2.0", "id": reqid, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})["result"]

    def test_initialize_discovery(self):
        response = handle(self.store, {"jsonrpc": "2.0", "id": 1,
                                       "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        response = handle(self.store, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(response["result"]["tools"]), len(TOOLS))
        self.assertNotIn("one_approve_task", [t["name"] for t in TOOLS])
        self.assertNotIn("one_run_cli", [t["name"] for t in TOOLS])
        self.assertIsNone(handle(self.store, {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_end_to_end_mcp_messages_and_task(self):
        row = self.request("one_create_task", {"title": "Audit", "goal": "Review P0-1", "assignee": "codex"})
        task = json.loads(row["content"][0]["text"])
        self.assertEqual(task["approval"], "pending")
        row = self.request("one_send_message", {"task_id": task["id"], "sender": "codex",
                                                     "recipient": "claude", "body": "Review my findings"})
        self.assertFalse(row["isError"])
        inbox = self.request("one_inbox", {"recipient": "claude"})
        self.assertEqual(len(json.loads(inbox["content"][0]["text"])), 1)

    def test_bad_args_and_json_errors(self):
        self.assertTrue(self.request("one_create_task", {"assignee": "codex"})["isError"])
        self.assertTrue(self.request("one_create_task", {"title": 44, "goal": "x", "assignee": "codex"})["isError"])
        self.assertTrue(self.request("one_inbox", {"recipient": "claude", "unknown": "x"})["isError"])
        self.assertEqual(handle(self.store, {"jsonrpc": "2.0", "id": 4,
                                             "method": "unknown"})["error"]["code"], -32601)
        self.assertEqual(handle(self.store, {"id": 1})["error"]["code"], -32600)

    def test_protocol_frames_and_bounded_line_size(self):
        source = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n" +
                               "{invalid\n" + json.dumps({"jsonrpc": "2.0", "id": 6,
                                                           "method": "tools/list"}) + "\n")
        output = io.StringIO()
        serve(source, output, store=self.store)
        results = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(results[0]["result"], {})
        self.assertEqual(results[1]["error"]["code"], -32700)
        self.assertGreater(len(results[2]["result"]["tools"]), 5)

    def test_mcp_initialize_with_whitespace_in_one_db_env(self):
        """Regression: real Copilot-style stdio spawn must survive outer whitespace."""
        target = Path(self.temp.name) / "copilot-state.db"
        env = dict(os.environ, ONE_DB="  " + str(target) + " \n")
        payload = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        ]
        proc = subprocess.run([sys.executable, "-m", "one_mcp.server"],
                              input="\n".join(json.dumps(x) for x in payload) + "\n",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=8, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = [json.loads(x) for x in proc.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertGreaterEqual(len(replies[1]["result"]["tools"]), 9)
        self.assertTrue(target.is_file())

    def test_mcp_startup_directory_path_fails_with_clear_error(self):
        """ONE_DB must never silently become a fresh database at another path."""
        env = dict(os.environ, ONE_DB=str(Path(self.temp.name)) + " ")
        proc = subprocess.run([sys.executable, "-m", "one_mcp.server"],
                              input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=8, text=True, env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SQLite database FILE", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_real_stdio_subprocess_persists_between_connections(self):
        env = dict(os.environ, ONE_DB=str(self.db))
        def conversation(payload):
            proc = subprocess.run([sys.executable, "-m", "one_mcp.server"],
                                  input="\n".join(json.dumps(x) for x in payload) + "\n",
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=8, text=True, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return [json.loads(x) for x in proc.stdout.splitlines()]
        first = conversation([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "one_create_task", "arguments": {
                    "title": "stdio", "goal": "Bridge smoke", "assignee": "codex", "request_key": "smoke-1"}}},
        ])
        self.assertEqual(len(first), 2)
        original_id = json.loads(first[1]["result"]["content"][0]["text"])["id"]
        again = conversation([
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "one_create_task", "arguments": {
                    "title": "stdio", "goal": "Bridge smoke", "assignee": "codex", "request_key": "smoke-1"}}},
        ])
        second_id = json.loads(again[0]["result"]["content"][0]["text"])["id"]
        self.assertEqual(original_id, second_id)


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from one_mcp import webhook
from one_mcp.store import Store

DUMMY_SECRET = "dummy-sender-key-for-tests"
WEBHOOK_VARS = ("ONE_WEBHOOK_URL", "ONE_WEBHOOK_SECRET", "ONE_WEBHOOK_AUTH_HEADER", "ONE_WEBHOOK_EVENTS")


class Stub:
    """Local 127.0.0.1 webhook receiver recording every POST."""

    def __init__(self, delay=0.0, status=204):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                stub.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "json": json.loads(body.decode("utf-8"))})
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/hook"


def clean_env(**values):
    env = {k: v for k, v in os.environ.items() if k not in WEBHOOK_VARS}
    env.update(values)
    return env


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "one.db"
        self.store = Store(self.db)
        self.stub = Stub()
        self.stderr = io.StringIO()

    def tearDown(self):
        webhook.flush()
        self.store.close()
        self.stub.close()
        self.temp.cleanup()

    @contextlib.contextmanager
    def env(self, **values):
        with mock.patch.dict(os.environ, clean_env(**values), clear=True), \
                contextlib.redirect_stderr(self.stderr):
            yield

    def running_task(self):
        task = self.store.create_task("Hook test", "Exercise webhook", "codex", owner="grok-bot")
        self.store.approve(task["id"], "approved")
        return task

    def test_disabled_when_url_unset(self):
        stdout = io.StringIO()
        with self.env(ONE_WEBHOOK_SECRET=DUMMY_SECRET), contextlib.redirect_stdout(stdout):
            self.assertFalse(webhook.enabled())
            task = self.running_task()
            self.store.claim_next("codex")
            self.store.complete(task["id"], "codex", "completed", "done")
            self.store.message(task["id"], "codex", "grok-bot", "finished")
            self.assertIsNone(webhook.notify(webhook.MESSAGE, self.store.task(task["id"])))
            webhook.flush()
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_status_change_payload(self):
        with self.env(ONE_WEBHOOK_URL=self.stub.url):
            task = self.running_task()
            self.store.claim_next("codex")
            self.store.complete(task["id"], "codex", "completed", "CLI exited 0")
            webhook.flush()
        self.assertEqual(len(self.stub.requests), 2)  # running, completed; exactly one per write
        # Delivery runs on independent daemon threads; HTTP arrival order is not guaranteed.
        by_status = {record["json"]["new_status"]: record for record in self.stub.requests}
        self.assertEqual(set(by_status), {"running", "completed"})
        sent = by_status["completed"]["json"]
        self.assertEqual(sent["event"], "task.status_changed")
        self.assertEqual((sent["old_status"], sent["new_status"], sent["status"]), ("running", "completed", "completed"))
        self.assertEqual((sent["task_id"], sent["title"], sent["assignee"], sent["owner"]),
                         (task["id"], "Hook test", "codex", "grok-bot"))
        self.assertTrue(sent["timestamp"].endswith("Z"))
        self.assertNotIn("message", sent)
        self.assertEqual(by_status["running"]["json"]["new_status"], "running")
        self.assertEqual(by_status["completed"]["headers"]["Content-Type"], "application/json")
        self.assertNotIn("Authorization", self.stub.requests[1]["headers"])

    def test_cancel_and_lease_recovery_fire(self):
        with self.env(ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_EVENTS="status"):
            queued = self.store.create_task("Cancel me", "x", "codex")
            self.store.cancel(queued["id"])
            task = self.running_task()
            self.store.claim_next("codex", lease_seconds=1)
            self.store.recover_expired(now=time.time() + 10)
            webhook.flush()
        transitions = [(r["json"]["old_status"], r["json"]["new_status"]) for r in self.stub.requests]
        self.assertCountEqual(transitions, [("queued", "cancelled"), ("queued", "running"), ("running", "blocked")])

    def test_message_payload_and_truncation(self):
        with self.env(ONE_WEBHOOK_URL=self.stub.url):
            task = self.store.create_task("Hook test", "Exercise webhook", "claude", owner="grok-bot")
            self.store.message(task["id"], "claude", "grok-bot", "word " * 1000)
            webhook.flush()
        self.assertEqual(len(self.stub.requests), 1)
        sent = self.stub.requests[0]["json"]
        self.assertEqual(sent["event"], "task.message")
        self.assertEqual(sent["task_id"], task["id"])
        self.assertEqual(sent["message"]["sender"], "claude")
        self.assertEqual(sent["message"]["recipient"], "grok-bot")
        self.assertEqual(len(sent["message"]["text"]), webhook.MAX_TEXT)
        self.assertTrue(sent["message"]["truncated"])
        self.assertTrue(sent["message"]["id"].startswith("MSG-"))
        self.assertNotIn("old_status", sent)

    def test_event_filter(self):
        with self.env(ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_EVENTS="status:completed, status:blocked"):
            task = self.running_task()
            self.store.claim_next("codex")
            self.store.complete(task["id"], "codex", "completed", "ok")
            self.store.message(task["id"], "codex", "grok-bot", "done")
            webhook.flush()
        self.assertEqual([r["json"]["new_status"] for r in self.stub.requests], ["completed"])
        with self.env(ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_EVENTS="message"):
            self.store.message(task["id"], "codex", "grok-bot", "again")
            webhook.flush()
        self.assertEqual(self.stub.requests[-1]["json"]["event"], "task.message")
        self.assertEqual(len(self.stub.requests), 2)

    def test_auth_header_default_bearer_and_custom(self):
        with self.env(ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_SECRET=DUMMY_SECRET):
            task = self.store.create_task("Hook test", "x", "codex")
            self.store.message(task["id"], "codex", "grok-bot", "hi")
            webhook.flush()
        self.assertEqual(self.stub.requests[0]["headers"]["Authorization"], "Bearer " + DUMMY_SECRET)
        with self.env(ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_SECRET=DUMMY_SECRET,
                      ONE_WEBHOOK_AUTH_HEADER="X-Sender-Key"):
            self.store.message(task["id"], "codex", "grok-bot", "hi")
            webhook.flush()
        headers = self.stub.requests[1]["headers"]
        self.assertEqual(headers["X-Sender-Key"], DUMMY_SECRET)
        self.assertNotIn("Authorization", headers)
        for request in self.stub.requests:
            self.assertNotIn(DUMMY_SECRET, json.dumps(request["json"]))

    def test_db_write_succeeds_when_endpoint_down(self):
        stdout = io.StringIO()
        with self.env(ONE_WEBHOOK_URL=closed_port_url(), ONE_WEBHOOK_SECRET=DUMMY_SECRET), \
                contextlib.redirect_stdout(stdout):
            task = self.running_task()
            self.store.claim_next("codex")
            done = self.store.complete(task["id"], "codex", "completed", "ok")
            msg = self.store.message(task["id"], "codex", "grok-bot", "done")
            webhook.flush()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.store.inbox("grok-bot")[0]["id"], msg["id"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("delivery of", self.stderr.getvalue())
        self.assertNotIn(DUMMY_SECRET, self.stderr.getvalue())

    def test_slow_endpoint_does_not_block_write(self):
        slow = Stub(delay=webhook.TIMEOUT_SECONDS + 1)
        try:
            with self.env(ONE_WEBHOOK_URL=slow.url):
                task = self.store.create_task("Hook test", "x", "codex")
                started = time.monotonic()
                self.store.message(task["id"], "codex", "grok-bot", "slow")
                elapsed = time.monotonic() - started
                webhook.flush()
            self.assertLess(elapsed, 1.0)
            self.assertEqual(len(self.store.inbox("grok-bot")), 1)
            self.assertIn("TimeoutError", self.stderr.getvalue())
        finally:
            slow.close()

    def test_error_status_and_redirect_are_not_followed(self):
        failing = Stub(status=302)
        try:
            with self.env(ONE_WEBHOOK_URL=failing.url, ONE_WEBHOOK_SECRET=DUMMY_SECRET):
                task = self.store.create_task("Hook test", "x", "codex")
                self.store.message(task["id"], "codex", "grok-bot", "hi")
                webhook.flush()
            self.assertEqual(len(failing.requests), 1)
            self.assertIn("HTTP 302", self.stderr.getvalue())
        finally:
            failing.close()

    def test_mcp_stdio_server_stdout_stays_protocol_only(self):
        env = clean_env(ONE_DB=str(self.db), ONE_WEBHOOK_URL=self.stub.url, ONE_WEBHOOK_SECRET=DUMMY_SECRET)
        task = self.store.create_task("Hook test", "x", "codex")
        payload = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "one_send_message", "arguments": {
                "task_id": task["id"], "sender": "codex", "recipient": "grok-bot", "body": "via mcp"}}},
        ]
        proc = subprocess.run([sys.executable, "-m", "one_mcp.server"],
                              input="\n".join(json.dumps(x) for x in payload) + "\n",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(json.loads(line)["jsonrpc"] == "2.0" for line in lines))
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(self.stub.requests[0]["json"]["message"]["text"], "via mcp")
        self.assertNotIn(DUMMY_SECRET, proc.stderr)

    def test_cli_message_delivers_before_exit(self):
        env = clean_env(ONE_DB=str(self.db), ONE_WEBHOOK_URL=self.stub.url)
        task = self.store.create_task("Hook test", "x", "codex")
        proc = subprocess.run([sys.executable, "-m", "one_mcp.cli", "message", task["id"], "codex", "grok-bot", "via cli"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["body"], "via cli")
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(self.stub.requests[0]["json"]["event"], "task.message")


if __name__ == "__main__":
    unittest.main()

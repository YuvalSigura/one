import concurrent.futures
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from one_mcp.store import Store, state_path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "one.db"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, **kwargs):
        return self.store.create_task("P0 evidence", "Validate results", "codex", **kwargs)

    def test_one_db_env_trims_accidental_outer_whitespace(self):
        value = f"  {self.db}  \n"
        with patch.dict("os.environ", {"ONE_DB": value}):
            self.assertEqual(state_path(), self.db)
            with Store() as connected:
                self.assertEqual(connected.path, self.db)

    def test_one_db_env_rejects_blank_or_controls(self):
        for value in ("   ", "\t \n ", str(self.db) + "\x00wrong",
                      str(self.db) + "\nother"):
            with self.subTest(value=repr(value)):
                with patch.dict("os.environ", {"ONE_DB": value}):
                    with self.assertRaisesRegex(ValueError, "ONE_DB"):
                        state_path()

    def test_one_db_env_rejects_directory_not_sqlite_file(self):
        with patch.dict("os.environ", {"ONE_DB": f"  {self.temp.name}  "}):
            with self.assertRaisesRegex(ValueError, "SQLite database FILE"):
                Store()
        self.assertFalse((Path(self.temp.name) / "state.db").exists())

    def test_idempotency_same_key_and_conflict(self):
        first = self.task(request_key="CEO-1")
        self.assertEqual(self.task(request_key="CEO-1")["id"], first["id"])
        self.assertEqual(len(self.store.list_tasks()), 1)
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.store.create_task("Other", "Different", "codex", request_key="CEO-1")

    def test_approval_claim_completion_and_no_double_dispatch(self):
        task = self.task()
        self.assertIsNone(self.store.claim_next("codex"))
        self.store.approve(task["id"], "approved")
        claimed = self.store.claim_next("codex")
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempt"], 1)
        self.assertIsNone(self.store.claim_next("codex"))
        self.store.complete(task["id"], "codex", "completed", "CLI exited 0; not proof")
        with self.assertRaises(ValueError):
            self.store.complete(task["id"], "codex", "completed", "replay")
        self.assertEqual(self.store.task(task["id"])["status"], "completed")

    def test_expired_lease_blocks_instead_of_repeating_paid_run(self):
        task = self.task()
        self.store.approve(task["id"], "approved")
        self.store.claim_next("codex", lease_seconds=10)
        expired = self.store.recover_expired(now=10**13)
        self.assertEqual(expired, [task["id"]])
        self.assertEqual(self.store.task(task["id"])["status"], "blocked")
        self.assertIsNone(self.store.claim_next("codex"))
        self.assertIn("lease_expired_blocked", [x["kind"] for x in self.store.events(task["id"])])

    def test_inbox_ack_and_redaction(self):
        task = self.task()
        record = self.store.message(task["id"], "codex", "claude", "Bearer secret-token-goes-here")
        self.assertNotIn("secret-token", record["body"])
        self.assertEqual(len(self.store.inbox("claude")), 1)
        self.assertEqual(len(self.store.inbox("codex")), 0)
        self.store.ack(record["id"], "claude")
        self.assertEqual(self.store.inbox("claude"), [])
        self.assertEqual(len(self.store.inbox("claude", unread_only=False)), 1)
        with self.assertRaises(ValueError):
            self.store.ack(record["id"], "codex")

    def test_path_validation_and_sensitive_output(self):
        with self.assertRaises(ValueError):
            self.task(allowed_paths=["../.env"])
        with self.assertRaises(ValueError):
            self.task(allowed_paths=["C:/temp"])
        with self.assertRaises(ValueError):
            self.task(allowed_paths=["src\\test.py"])
        with self.assertRaises(ValueError):
            self.task(budget=-1)
        task = self.task()
        self.assertIsNone(self.store.task(task["id"])["workspace"])

    def test_migration_from_previous_mvp(self):
        other = Path(self.temp.name) / "old.db"
        connection = sqlite3.connect(other)
        connection.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY,title TEXT,goal TEXT,assignee TEXT,status TEXT,
           owner TEXT,base_commit TEXT,allowed_paths TEXT,acceptance TEXT,budget INTEGER,result TEXT,
           created REAL,updated REAL);
        INSERT INTO tasks VALUES ('TASK-old','old','retain','codex','queued','grok-bot','',
          '[]','[]',0,NULL,1,1);
        CREATE TABLE messages (id TEXT PRIMARY KEY,task_id TEXT,sender TEXT,recipient TEXT,body TEXT,created REAL);
        CREATE TABLE artifacts (id TEXT PRIMARY KEY,task_id TEXT,producer TEXT,kind TEXT,path TEXT,digest TEXT,created REAL);
        """)
        connection.commit()
        connection.close()
        with Store(other) as upgraded:
            self.assertEqual(upgraded.task("TASK-old")["approval"], "pending")
            upgraded.approve("TASK-old", "approved")
            self.assertEqual(upgraded.task("TASK-old")["approval"], "approved")

    def test_concurrent_workers_claim_once(self):
        task = self.task()
        self.store.approve(task["id"], "approved")
        def claim(_):
            with Store(self.db) as other:
                return other.claim_next("codex")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(claim, range(3)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_artifact_metadata_not_verified_proof(self):
        task = self.task()
        receipt = self.store.add_artifact(task["id"], "codex", "diff", "changes.diff", "f"*64)
        self.assertEqual(receipt["digest"], "f"*64)
        with self.assertRaises(ValueError):
            self.store.add_artifact(task["id"], "codex", "diff", "changes.diff", "wrong")


if __name__ == "__main__":
    unittest.main()

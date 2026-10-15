"""SQLite-backed shared task, message and artifact metadata. No target secrets."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

STATES = {"queued", "claimed", "completed", "failed", "blocked"}
AGENTS = {"grok-bot", "grok-cli", "claude", "codex", "operator"}

def state_path():
    return Path(os.environ.get("ONE_DB", str(Path.home() / ".one" / "state.db"))).expanduser()

class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL,
          assignee TEXT NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
          base_commit TEXT, allowed_paths TEXT NOT NULL, acceptance TEXT NOT NULL,
          budget INTEGER NOT NULL, result TEXT, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sender TEXT NOT NULL,
          recipient TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, producer TEXT NOT NULL,
          kind TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
          created REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE INDEX IF NOT EXISTS messages_recipient ON messages(recipient,created);
        """)
    def close(self):
        self.db.close()
    def _one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None
    def task(self, task_id):
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["allowed_paths"] = json.loads(row["allowed_paths"])
            row["acceptance"] = json.loads(row["acceptance"])
        return row
    def create_task(self, title, goal, assignee, owner="grok-bot", base_commit="", allowed_paths=None, acceptance=None, budget=0):
        if assignee not in AGENTS or owner not in AGENTS:
            raise ValueError("unknown agent")
        if not title.strip() or not goal.strip():
            raise ValueError("title and goal required")
        if not isinstance(budget, int) or budget < 0:
            raise ValueError("budget must be nonnegative integer")
        paths = allowed_paths or []
        checks = acceptance or []
        if not isinstance(paths, list) or not all(isinstance(x,str) for x in paths):
            raise ValueError("allowed_paths must be strings")
        if not isinstance(checks, list) or not all(isinstance(x,str) for x in checks):
            raise ValueError("acceptance must be strings")
        tid = "TASK-" + uuid.uuid4().hex[:16]
        now = time.time()
        self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid,title,goal,assignee,"queued",owner,base_commit,json.dumps(paths),json.dumps(checks),budget,None,now,now))
        return self.task(tid)
    def list_tasks(self, assignee=None, status=None, limit=30):
        if assignee is not None and assignee not in AGENTS:
            raise ValueError("unknown agent")
        if status is not None and status not in STATES:
            raise ValueError("unknown status")
        limit = max(1,min(int(limit),100))
        rows = self.db.execute("SELECT id FROM tasks WHERE (? IS NULL OR assignee=?) AND (? IS NULL OR status=?) ORDER BY created DESC LIMIT ?",
            (assignee,assignee,status,status,limit)).fetchall()
        return [self.task(r["id"]) for r in rows]
    def claim(self, task_id, agent):
        if agent not in AGENTS:
            raise ValueError("unknown agent")
        cursor = self.db.execute("UPDATE tasks SET status='claimed',updated=? WHERE id=? AND assignee=? AND status='queued'",
            (time.time(),task_id,agent))
        if cursor.rowcount != 1:
            raise ValueError("task not queued or not assigned to agent")
        return self.task(task_id)
    def finish(self, task_id, agent, status, result):
        if status not in {"completed","failed","blocked"}:
            raise ValueError("invalid terminal status")
        if agent not in AGENTS:
            raise ValueError("unknown agent")
        cursor = self.db.execute("UPDATE tasks SET status=?,result=?,updated=? WHERE id=? AND assignee=? AND status='claimed'",
            (status,str(result)[:16000],time.time(),task_id,agent))
        if cursor.rowcount != 1:
            raise ValueError("task not claimed by agent")
        return self.task(task_id)
    def message(self, task_id, sender, recipient, body):
        if sender not in AGENTS or recipient not in AGENTS:
            raise ValueError("unknown agent")
        if not self.task(task_id):
            raise ValueError("unknown task")
        if not isinstance(body,str) or not body.strip() or len(body)>16000:
            raise ValueError("message must contain 1..16000 characters")
        mid = "MSG-" + uuid.uuid4().hex[:16]
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
            (mid,task_id,sender,recipient,body,time.time()))
        return self._one("SELECT * FROM messages WHERE id=?", (mid,))
    def inbox(self, recipient, limit=30):
        if recipient not in AGENTS:
            raise ValueError("unknown agent")
        limit = max(1,min(int(limit),100))
        return [dict(r) for r in self.db.execute("SELECT * FROM messages WHERE recipient=? ORDER BY created DESC LIMIT ?",(recipient,limit))]
    def add_artifact(self, task_id, producer, kind, path, digest):
        if producer not in AGENTS or not self.task(task_id):
            raise ValueError("unknown producer or task")
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("digest must be lowercase sha256")
        if not kind or not path or len(path)>2048:
            raise ValueError("kind and path required")
        aid="ART-"+uuid.uuid4().hex[:16]
        self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
            (aid,task_id,producer,kind,path,digest,time.time()))
        return self._one("SELECT * FROM artifacts WHERE id=?", (aid,))
    def artifacts(self, task_id):
        if not self.task(task_id):
            raise ValueError("unknown task")
        return [dict(r) for r in self.db.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created",(task_id,))]

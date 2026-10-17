"""Durable SQLite task, message, event and artifact metadata (not a proof store)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from . import webhook
from .safety import bounded, redact, validate_paths, ensure_digest, MAX_GOAL, MAX_TITLE, MAX_MESSAGE, MAX_RESULT, MAX_ARTIFACT_PATH

AGENTS = frozenset({"grok-bot", "grok-cli", "claude", "codex", "antigravity", "operator"})
STATUS = frozenset({"queued", "running", "completed", "failed", "blocked", "cancelled"})
TERMINAL = frozenset({"completed", "failed", "blocked", "cancelled"})


def state_path() -> Path:
    return Path(os.getenv("ONE_DB", str(Path.home() / ".one" / "state.db"))).expanduser()


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          goal TEXT NOT NULL,
          assignee TEXT NOT NULL,
          status TEXT NOT NULL,
          owner TEXT NOT NULL,
          base_commit TEXT NOT NULL DEFAULT '',
          allowed_paths TEXT NOT NULL DEFAULT '[]',
          acceptance TEXT NOT NULL DEFAULT '[]',
          budget INTEGER NOT NULL DEFAULT 0,
          result TEXT,
          created REAL NOT NULL,
          updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, sender TEXT NOT NULL,
          recipient TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE INDEX IF NOT EXISTS messages_inbox ON messages(recipient, created);
        CREATE TABLE IF NOT EXISTS artifacts (
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL, producer TEXT NOT NULL,
          kind TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
          created REAL NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        """)
        self._migrate()

    def _migrate(self):
        # Preserve installed v0.1 databases. Migrations are additive and idempotent.
        additions = {
            "request_key": "TEXT",
            "approval": "TEXT NOT NULL DEFAULT 'pending'",
            "approved_at": "REAL",
            "lease_deadline": "REAL",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "workspace": "TEXT",
            "usage_json": "TEXT",
            "instructions_digest": "TEXT",
        }
        existing = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        for column, ddl in additions.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
        self.db.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS tasks_request_key ON tasks(request_key)
          WHERE request_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(assignee,approval,status,created);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id TEXT NOT NULL, kind TEXT NOT NULL,
          data TEXT NOT NULL, created REAL NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS inbox_ack (
          message_id TEXT NOT NULL, recipient TEXT NOT NULL, read_at REAL NOT NULL,
          PRIMARY KEY(message_id,recipient),
          FOREIGN KEY(message_id) REFERENCES messages(id)
        );
        CREATE TABLE IF NOT EXISTS flow_plans (
          id TEXT PRIMARY KEY,
          description TEXT NOT NULL,
          project_context TEXT NOT NULL DEFAULT '',
          plan_digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flow_steps (
          flow_id TEXT NOT NULL,
          step_key TEXT NOT NULL,
          task_id TEXT NOT NULL UNIQUE,
          role TEXT NOT NULL,
          step_order INTEGER NOT NULL,
          context TEXT NOT NULL DEFAULT '',
          handoff_mode TEXT NOT NULL DEFAULT 'focused_review',
          must_know TEXT NOT NULL DEFAULT '{}',
          required_handoff TEXT NOT NULL DEFAULT '[]',
          required_checks TEXT NOT NULL DEFAULT '[]',
          PRIMARY KEY(flow_id,step_key),
          FOREIGN KEY(flow_id) REFERENCES flow_plans(id),
          FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS flow_dependencies (
          flow_id TEXT NOT NULL,
          step_key TEXT NOT NULL,
          upstream_key TEXT NOT NULL,
          gate TEXT NOT NULL CHECK (gate IN ('completed','accepted')),
          PRIMARY KEY(flow_id,step_key,upstream_key),
          FOREIGN KEY(flow_id) REFERENCES flow_plans(id)
        );
        CREATE TABLE IF NOT EXISTS flow_verdicts (
          task_id TEXT PRIMARY KEY,
          decision TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
          reason TEXT NOT NULL,
          FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS flow_authorizations (
          flow_id TEXT PRIMARY KEY,
          plan_digest TEXT NOT NULL,
          authorized_at REAL NOT NULL,
          revoked_at REAL,
          FOREIGN KEY(flow_id) REFERENCES flow_plans(id)
        );
        CREATE INDEX IF NOT EXISTS flow_steps_by_task ON flow_steps(task_id);
        CREATE INDEX IF NOT EXISTS flow_dependencies_step ON flow_dependencies(flow_id,step_key);
        """)
        # Existing users can have a pilot database created by earlier commits
        # of this feature branch; upgrade it in place without losing tasks.
        flow_columns = {row[1] for row in self.db.execute("PRAGMA table_info(flow_steps)")}
        for name, ddl in {
            "handoff_mode": "TEXT NOT NULL DEFAULT 'focused_review'",
            "must_know": "TEXT NOT NULL DEFAULT '{}'",
            "required_handoff": "TEXT NOT NULL DEFAULT '[]'",
            "required_checks": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in flow_columns:
                self.db.execute(f"ALTER TABLE flow_steps ADD COLUMN {name} {ddl}")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _notify(self, event, task_id, **fields):
        """Single hook for outbound webhooks (CLI, worker and MCP all write through Store).

        Called only after the write is durable: the connection is autocommit, and
        claim_next calls it after COMMIT. Never raises, never writes to stdout."""
        if not webhook.enabled():
            return
        if self.db.in_transaction:
            webhook._log(f"skipped {event}: called inside an open transaction")
            return
        try:
            webhook.notify(event, self.task(task_id), **fields)
        except Exception as exc:
            webhook._log(f"skipped {event}: {type(exc).__name__}")

    def _one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _event(self, task_id, kind, data=None):
        self.db.execute("INSERT INTO events(task_id,kind,data,created) VALUES(?,?,?,?)",
                        (task_id, kind, json.dumps(data or {}, ensure_ascii=False), time.time()))

    def task(self, task_id):
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["allowed_paths"] = json.loads(row["allowed_paths"])
            row["acceptance"] = json.loads(row["acceptance"])
            row["usage_json"] = json.loads(row["usage_json"]) if row["usage_json"] else None
        return row

    def create_task(self, title, goal, assignee, owner="grok-bot", base_commit="", allowed_paths=None,
                    acceptance=None, budget=0, request_key=None):
        if assignee not in AGENTS or owner not in AGENTS:
            raise ValueError("unknown agent")
        title = bounded(title, MAX_TITLE, "title")
        goal = redact(bounded(goal, MAX_GOAL, "goal"))
        if not isinstance(budget, int) or budget < 0 or budget > 1000000:
            raise ValueError("budget must be integer between 0 and 1000000")
        allowed_paths = validate_paths(allowed_paths or [])
        acceptance = acceptance or []
        if not isinstance(acceptance, list) or len(acceptance) > 30 or any(not isinstance(v, str) or len(v) > 300 for v in acceptance):
            raise ValueError("acceptance must contain <= 30 bounded strings")
        if not isinstance(base_commit, str) or len(base_commit) > 80:
            raise ValueError("invalid base commit")
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
            raise ValueError("invalid request_key")
        fingerprint = (title, goal, assignee, owner, base_commit, allowed_paths, acceptance, budget)
        if request_key:
            previous = self._one("SELECT id FROM tasks WHERE request_key=?", (request_key,))
            if previous:
                found = self.task(previous["id"])
                previous_fingerprint = (found["title"], found["goal"], found["assignee"], found["owner"],
                                        found["base_commit"], found["allowed_paths"], found["acceptance"], found["budget"])
                if previous_fingerprint != fingerprint:
                    raise ValueError("request_key conflict: task body differs")
                return found
        now = time.time()
        task_id = "TASK-" + uuid.uuid4().hex[:16]
        try:
            self.db.execute("""INSERT INTO tasks (
              id,title,goal,assignee,status,owner,base_commit,allowed_paths,acceptance,budget,created,updated,request_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (task_id, title, goal, assignee, "queued", owner, base_commit,
                             json.dumps(allowed_paths), json.dumps(acceptance), budget, now, now, request_key))
        except sqlite3.IntegrityError as exc:
            if request_key:
                existing = self._one("SELECT id FROM tasks WHERE request_key=?", (request_key,))
                if existing:
                    return self.create_task(title, goal, assignee, owner, base_commit, allowed_paths,
                                            acceptance, budget, request_key)
            raise ValueError("task creation conflict") from exc
        self._event(task_id, "created", {"assignee": assignee})
        return self.task(task_id)

    def list_tasks(self, assignee=None, status=None, limit=25):
        if assignee is not None and assignee not in AGENTS:
            raise ValueError("unknown agent")
        if status is not None and status not in STATUS:
            raise ValueError("unknown status")
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute("""SELECT id FROM tasks
            WHERE (? IS NULL OR assignee=?) AND (? IS NULL OR status=?)
            ORDER BY created DESC LIMIT ?""", (assignee, assignee, status, status, limit)).fetchall()
        return [self.task(row["id"]) for row in rows]

    def approve(self, task_id, decision):
        """Operator CLI only; intentionally NEVER exported as an MCP tool."""
        if decision not in ("approved", "rejected"):
            raise ValueError("approval must be approved or rejected")
        cursor = self.db.execute("""UPDATE tasks SET approval=?,approved_at=?,updated=?
            WHERE id=? AND status='queued' AND approval='pending'""",
                                 (decision, time.time(), time.time(), task_id))
        if cursor.rowcount != 1:
            raise ValueError("task not awaiting approval")
        self._event(task_id, decision)
        return self.task(task_id)

    def cancel(self, task_id):
        """Only cancel queued tasks. Running processes require explicit worker termination."""
        cursor = self.db.execute("UPDATE tasks SET status='cancelled',updated=? WHERE id=? AND status='queued'",
                                 (time.time(), task_id))
        if cursor.rowcount != 1:
            raise ValueError("only queued tasks can be cancelled")
        self._event(task_id, "cancelled")
        self._notify(webhook.STATUS_CHANGED, task_id, old_status="queued", new_status="cancelled")
        return self.task(task_id)

    def claim_next(self, agent, lease_seconds=1800, workflow_id=None):
        if agent not in AGENTS - {"operator", "grok-bot"}:
            raise ValueError("no automated worker for this agent")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease must be 1..3600 seconds")
        # BEGIN IMMEDIATE atomically selects and claims across worker processes.
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._one("""SELECT tasks.id FROM tasks WHERE tasks.status='queued'
                AND tasks.approval='approved' AND tasks.assignee=?
                AND (? IS NULL OR EXISTS (
                  SELECT 1 FROM flow_steps scoped
                  WHERE scoped.task_id=tasks.id AND scoped.flow_id=?
                ))
                AND NOT EXISTS (
                  SELECT 1 FROM flow_steps scoped
                  JOIN flow_plans plan ON plan.id=scoped.flow_id
                  LEFT JOIN flow_authorizations grant_row ON grant_row.flow_id=scoped.flow_id
                  WHERE scoped.task_id=tasks.id
                    AND (grant_row.flow_id IS NULL OR grant_row.revoked_at IS NOT NULL
                         OR grant_row.plan_digest!=plan.plan_digest)
                )
                AND NOT EXISTS (
                  SELECT 1 FROM flow_steps successor
                  JOIN flow_dependencies d
                    ON d.flow_id=successor.flow_id AND d.step_key=successor.step_key
                  JOIN flow_steps predecessor
                    ON predecessor.flow_id=d.flow_id AND predecessor.step_key=d.upstream_key
                  JOIN tasks predecessor_task ON predecessor_task.id=predecessor.task_id
                  LEFT JOIN flow_verdicts v ON v.task_id=predecessor.task_id
                  WHERE successor.task_id=tasks.id AND (
                    predecessor_task.status!='completed' OR
                    (d.gate='accepted' AND COALESCE(v.decision,'')!='accepted')
                  )
                )
                ORDER BY tasks.created LIMIT 1""", (agent, workflow_id, workflow_id))
            if not row:
                self.db.execute("COMMIT")
                return None
            now = time.time()
            self.db.execute("""UPDATE tasks SET status='running',attempt=attempt+1,
                lease_deadline=?,updated=? WHERE id=? AND status='queued'""",
                            (now + lease_seconds, now, row["id"]))
            self._event(row["id"], "started", {"agent": agent})
            self.db.execute("COMMIT")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        self._notify(webhook.STATUS_CHANGED, row["id"], old_status="queued", new_status="running")
        return self.task(row["id"])

    def complete(self, task_id, agent, status, result, workspace=None, usage=None, artifact_digest=None):
        if agent not in AGENTS or status not in {"completed", "failed", "blocked"}:
            raise ValueError("invalid agent or terminal status")
        result = redact(bounded(result, MAX_RESULT, "result"))
        if artifact_digest is not None:
            ensure_digest(artifact_digest)
        cursor = self.db.execute("""UPDATE tasks SET status=?,result=?,workspace=?,usage_json=?,
            instructions_digest=?,lease_deadline=NULL,updated=?
            WHERE id=? AND assignee=? AND status='running'""",
            (status, result, str(workspace) if workspace else None,
             json.dumps(usage) if usage is not None else None, artifact_digest,
             time.time(), task_id, agent))
        if cursor.rowcount != 1:
            raise ValueError("task is not running under this agent")
        self._event(task_id, status, {"agent": agent, "workspace": str(workspace or "")})
        self._notify(webhook.STATUS_CHANGED, task_id, old_status="running", new_status=status)
        return self.task(task_id)

    def recover_expired(self, now=None):
        """Fail closed on worker crash: NEVER automatically repeat a paid model call."""
        now = now or time.time()
        rows = self.db.execute("SELECT id FROM tasks WHERE status='running' AND lease_deadline < ?", (now,)).fetchall()
        for row in rows:
            cursor = self.db.execute("""UPDATE tasks SET status='blocked',result=?,lease_deadline=NULL,updated=?
                WHERE id=? AND status='running' AND lease_deadline < ?""",
                ("Worker lease expired. Inspect logs/processes before any manual retry.", now, row["id"], now))
            if self.task(row["id"])["status"] == "blocked":
                self._event(row["id"], "lease_expired_blocked")
            if cursor.rowcount == 1:
                self._notify(webhook.STATUS_CHANGED, row["id"], old_status="running", new_status="blocked")
        return [r["id"] for r in rows]

    def message(self, task_id, sender, recipient, body):
        if sender not in AGENTS or recipient not in AGENTS:
            raise ValueError("unknown agent")
        if not self.task(task_id):
            raise ValueError("unknown task")
        body = redact(bounded(body, MAX_MESSAGE, "message"))
        message_id = "MSG-" + uuid.uuid4().hex[:16]
        self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)",
                        (message_id, task_id, sender, recipient, body, time.time()))
        stored = self._one("SELECT * FROM messages WHERE id=?", (message_id,))
        self._notify(webhook.MESSAGE, task_id, message=stored)
        return stored

    def inbox(self, recipient, limit=25, unread_only=True):
        if recipient not in AGENTS:
            raise ValueError("unknown agent")
        limit = max(1, min(int(limit), 100))
        rows = self.db.execute("""SELECT m.* FROM messages m
            LEFT JOIN inbox_ack a ON a.message_id=m.id AND a.recipient=?
            WHERE m.recipient=? AND (?=0 OR a.message_id IS NULL)
            ORDER BY m.created DESC LIMIT ?""",
            (recipient, recipient, int(bool(unread_only)), limit))
        return [dict(r) for r in rows]

    def ack(self, message_id, recipient):
        if recipient not in AGENTS:
            raise ValueError("unknown agent")
        row = self._one("SELECT * FROM messages WHERE id=? AND recipient=?", (message_id, recipient))
        if not row:
            raise ValueError("unknown message for recipient")
        self.db.execute("INSERT OR IGNORE INTO inbox_ack VALUES (?,?,?)", (message_id, recipient, time.time()))
        return {"message_id": message_id, "acknowledged": True}

    def add_artifact(self, task_id, producer, kind, path, digest):
        if producer not in AGENTS or not self.task(task_id):
            raise ValueError("unknown producer or task")
        kind = bounded(kind, 60, "kind")
        path = bounded(path, MAX_ARTIFACT_PATH, "path")
        digest = ensure_digest(digest)
        artifact_id = "ART-" + uuid.uuid4().hex[:16]
        self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                        (artifact_id, task_id, producer, kind, path, digest, time.time()))
        return self._one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))

    def artifacts(self, task_id):
        if not self.task(task_id):
            raise ValueError("unknown task")
        return [dict(r) for r in self.db.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created", (task_id,))]

    def events(self, task_id, limit=50):
        if not self.task(task_id):
            raise ValueError("unknown task")
        limit = max(1, min(int(limit), 100))
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id, limit))]

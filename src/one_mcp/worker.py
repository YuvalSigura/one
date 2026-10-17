"""Operator-started, explicit-approval, isolated CLI execution worker.

Not exposed through MCP. No model launch without local approval + enabled local config.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .context import load_config, write_brief
from .roleflow import RoleFlow
from .receipts import make_manifest
from .patches import materialize_for_tester, classify_test_changes
from .safety import redact
from .store import Store
from .worktrees import isolate, _git

EXECUTABLES = {
    "claude": {"claude", "claude.exe", "claude.cmd"},
    "codex": {"codex", "codex.exe", "codex.cmd"},
    "grok-cli": {"grok", "grok.exe", "grok.cmd", "grok-cli", "grok-cli.exe", "grok-cli.cmd"},
    "antigravity": {"agy", "agy.exe"},
}
FORBIDDEN_FLAGS = {"--yolo", "--dangerously-skip-permissions", "--full-auto", "--no-sandbox", "bypassPermissions"}


def validated_command(agent: str, row: dict, brief: Path) -> tuple[list[str], bytes | None, int]:
    if agent not in EXECUTABLES or not isinstance(row, dict) or row.get("enabled") is not True:
        raise ValueError("agent is not enabled in operator config")
    argv = row.get("argv")
    if not isinstance(argv, list) or len(argv) < 2 or not all(isinstance(x, str) and x for x in argv):
        raise ValueError("agent argv must be a reviewed list of nonempty strings")
    if Path(argv[0]).name.lower() not in EXECUTABLES[agent]:
        raise ValueError("wrong executable for agent")
    if any(any(flag.casefold() in value.casefold() for flag in FORBIDDEN_FLAGS) for value in argv):
        raise ValueError("unsafe permission bypass is forbidden")
    timeout = row.get("timeout_seconds", 600)
    if type(timeout) is not int or not 10 <= timeout <= 1800:
        raise ValueError("timeout_seconds must be between 10 and 1800")
    mode = row.get("input_mode")
    if mode not in ("stdin", "brief_argument"):
        raise ValueError("input_mode must be stdin or brief_argument")
    instruction = "Read the approved ONE development task at: " + str(brief) + "\n" + (
        "Carry out only that task within the current isolated worktree. "
        "Report exact changes and checks. Do not push, merge, or access secrets."
    )
    if mode == "brief_argument":
        if sum(a.count("{brief}") for a in argv) != 1:
            raise ValueError("brief_argument requires exactly one {brief} placeholder")
        argv = [x.replace("{brief}", instruction) for x in argv]
        return argv, None, timeout
    if any("{brief}" in x for x in argv):
        raise ValueError("stdin mode may not interpolate brief")
    return argv, instruction.encode("utf-8"), timeout


def _capture(argv: list[str], cwd: Path, stdin: bytes | None, timeout: int,
             stdout_path: Path, stderr_path: Path) -> tuple[int, bool]:
    """Bound memory (stream to local files); terminate on timeout. No shell."""
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            kwargs = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            proc = subprocess.run(argv, cwd=str(cwd), stdout=stdout, stderr=stderr,
                                  shell=False, check=False, timeout=timeout, **kwargs)
        return proc.returncode, False
    except subprocess.TimeoutExpired:
        return 124, True


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _tail_redacted(path: Path, limit=1500) -> str:
    with path.open("rb") as fp:
        fp.seek(0, 2)
        fp.seek(max(0, fp.tell() - limit))
        tail = fp.read().decode("utf-8", "replace")
    return redact(tail)[-limit:]


def _changes(worktree: Path) -> list[str]:
    # NUL termination correctly handles newline and unusual Git filenames.
    data = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain", "-z", "--untracked-files=all"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, timeout=30).stdout
    parts = data.split(b"\0")
    files = []
    pos = 0
    while pos < len(parts):
        item = parts[pos]
        pos += 1
        if not item:
            continue
        if len(item) < 4:
            raise ValueError("malformed git status")
        status = item[:2].decode("ascii", "replace")
        files.append(item[3:].decode("utf-8", "replace"))
        if "R" in status or "C" in status:
            pos += 1  # old filename follows for renames/copies
    return files


def _allowed(filename: str, prefixes: list[str]) -> bool:
    return any(filename == allowed.rstrip("/") or filename.startswith(allowed.rstrip("/") + "/")
               for allowed in prefixes)


def execute_one(store: Store, agent: str, config_path: str | Path, *, dry_run=False, workflow_id=None):
    """Process at most ONE approved task. Does not auto-retry after failure."""
    config = load_config(config_path)
    row = config["agents"].get(agent)
    if not isinstance(row, dict) or not row.get("enabled"):
        raise ValueError("agent disabled or not configured")
    repo = str(config.get("repository") or "")
    root = str(config.get("worktree_root") or "")
    artifacts_dir = str(config.get("artifact_root") or "")
    if not repo or not root or not artifacts_dir:
        raise ValueError("config must define repository, worktree_root and artifact_root")
    pending = [task for task in store.list_tasks(assignee=agent, status="queued", limit=100)
               if task["approval"] == "approved" and (
                   workflow_id is None or (RoleFlow(store)._step(task["id"]) or {}).get("flow_id") == workflow_id)]
    if dry_run:
        return {"status": "dry_run", "next_task": pending[-1]["id"] if pending else None,
                "agent": agent, "will_execute": False}
    # Validate operator-configured directories before acquiring the durable
    # task lease. Invalid local paths must never strand a queued task as
    # 'running' or consume an approval/billable attempt.
    for label, configured in (("repository", repo), ("worktree_root", root),
                              ("artifact_root", artifacts_dir)):
        try:
            checked = Path(configured).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"configured {label} directory does not exist") from exc
        if not checked.is_dir():
            raise ValueError(f"configured {label} must be a directory")
        if label == "artifact_root":
            artifact_dir = checked
    task = store.claim_next(agent, lease_seconds=min(int(row.get("timeout_seconds", 600)) + 120, 3600), workflow_id=workflow_id)
    if task is None:
        return {"status": "idle", "agent": agent}
    workspace = None
    brief_digest = None
    try:
        workspace = isolate(repo, root, task)
        context = RoleFlow(store).handoff(task["id"], artifact_dir)
        # Materialize the exact independently hashed implementer change into
        # this TESTER's isolated worktree; never test the stale base checkout.
        borrowed = materialize_for_tester(store, task, workspace, artifact_dir)
        if borrowed:
            context += ("\n## Test checkout fidelity\n"
                        "The upstream implementation snapshot is MATERIALIZED in your "
                        "current isolated worktree and must be treated as read-only. "
                        "Run tests against THIS checkout. Do not change borrowed source files.\n")
        brief, brief_digest = write_brief(task, artifact_dir, extra_context=context)
        argv, prompt, timeout = validated_command(agent, row, brief)
        task_dir = brief.parent
        stdout_path = task_dir / "stdout.log"
        stderr_path = task_dir / "stderr.log"
        exit_code, timed_out = _capture(argv, workspace, prompt, timeout, stdout_path, stderr_path)
        raw_changes = _changes(workspace)
        changes, altered_borrowed = classify_test_changes(workspace, borrowed, raw_changes)
        offending = sorted(set(altered_borrowed +
                         [f for f in changes if not _allowed(f, task["allowed_paths"])]))
        # Register exact hash of immutable-in-practice local log; do not inline into model context.
        for kind, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            digest = _hash_file(path)
            store.add_artifact(task["id"], agent, kind, str(path), digest)
        # Changed file list is metadata, never interpreted as verification of security findings.
        manifest = task_dir / "changes.json"
        manifest.write_text(json.dumps(make_manifest(workspace, changes, offending, stdout_path),
                                       ensure_ascii=False, indent=2), encoding="utf-8")
        store.add_artifact(task["id"], agent, "change_manifest", str(manifest),
                           _hash_file(manifest))
        if timed_out:
            status, summary = "blocked", "CLI timed out; inspect running children and logs before retry."
        elif offending:
            status, summary = "blocked", "CLI changed paths outside allowed_paths; quarantined in isolated worktree."
        elif exit_code != 0:
            status, summary = "failed", f"CLI exit code {exit_code}; inspect local stderr artifact."
        else:
            status, summary = "completed", "CLI exited 0. Changes require independent test and operator review."
        result = store.complete(task["id"], agent, status, summary, workspace=workspace,
                                artifact_digest=brief_digest)
        store.message(task["id"], agent, "grok-bot",
                      f"Task {task['id']} {status}. Review worktree and artifact refs before integration.")
        return {"task": result["id"], "status": status, "exit_code": exit_code,
                "changed_paths": changes[:100], "outside_allowlist": offending[:100],
                "workspace": str(workspace), "review_required": True}
    except Exception as exc:
        # Fail closed, and avoid putting exception messages (which may include credentials) in model context.
        store.complete(task["id"], agent, "blocked",
                       "Worker blocked before successful completion; operator must inspect local logs.",
                       workspace=workspace, artifact_digest=brief_digest)
        raise RuntimeError(f"worker blocked task {task['id']}: {type(exc).__name__}") from exc


def doctor(config_path: str | Path) -> dict:
    """Read-only check; does not launch CLIs or alter git repositories."""
    import shutil
    config = load_config(config_path)
    agents = {}
    for agent, row in config["agents"].items():
        if agent not in EXECUTABLES:
            continue
        argv = row.get("argv") if isinstance(row, dict) else None
        binary = argv[0] if isinstance(argv, list) and argv else ""
        agents[agent] = {"enabled": row.get("enabled") is True if isinstance(row, dict) else False,
                         "executable_found": bool(shutil.which(binary)),
                         "binary": Path(binary).name if binary else None}
    return {"agents": agents, "repository_exists": Path(str(config.get("repository", ""))).is_dir(),
            "worktree_root_exists": Path(str(config.get("worktree_root", ""))).is_dir(),
            "artifact_root_exists": Path(str(config.get("artifact_root", ""))).is_dir()}

"""Deterministic engineering checks — explicit local operator command only.

This is an engineering quality receipt, NOT scientific truth or review authority.
Never runs from MCP, never chooses model commands or arbitrary shell strings.
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

from .context import load_config
from .patches import file_digest, _safe_child
from .safety import redact
from .worktrees import _git

PROFILES = {
    "diff": ["git", "diff", "--check"],
    "unittest": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    "pytest": [sys.executable, "-m", "pytest", "-q"],
}
MAX_CHANGED = 200
MAX_FILE = 10_000_000


def checkout_fingerprint(workspace, base_commit):
    root = Path(workspace).resolve(strict=True)
    if _git(root, "rev-parse", "HEAD").stdout.strip() != base_commit:
        raise ValueError("quality checkout commit mismatch")
    from .worker import _changes
    changes = _changes(root)
    if len(changes) > MAX_CHANGED:
        raise ValueError("too many changed files for quality receipt")
    snapshot = {}
    for name in sorted(changes):
        path = _safe_child(root, name)
        if path.exists() and (not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE):
            raise ValueError("unsafe quality source file")
        digest = file_digest(path)
        snapshot[name] = digest  # None = tracked deletion
    serial = json.dumps({"base": base_commit, "files": snapshot},
                        sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serial).hexdigest()


def _verified_workspace(store, task_id, cfg):
    task = store.task(task_id)
    if task is None or task["status"] != "completed":
        raise ValueError("engineering quality requires a completed task")
    workspace = task.get("workspace")
    if not workspace:
        raise ValueError("task has no recorded worktree")
    path = Path(workspace).expanduser().resolve(strict=True)
    root = Path(cfg.get("worktree_root") or "").expanduser().resolve(strict=True)
    if path.parent != root or path.name != task_id:
        raise ValueError("worktree is outside configured task root")
    if not path.is_dir():
        raise ValueError("missing task checkout")
    return task, path


def run_quality(store, task_id, config_path, checks, *, timeout=120):
    if not isinstance(checks, list) or not checks or len(set(checks)) != len(checks):
        raise ValueError("provide 1..3 unique engineering checks")
    if any(p not in PROFILES for p in checks):
        raise ValueError("unsupported check; no arbitrary command execution")
    if type(timeout) is not int or not 10 <= timeout <= 300:
        raise ValueError("quality timeout must be 10..300 seconds")
    cfg = load_config(config_path)
    task, worktree = _verified_workspace(store, task_id, cfg)
    before = checkout_fingerprint(worktree, task["base_commit"])
    report = {"task_id": task_id, "base_commit": task["base_commit"],
              "checkout_before": before, "checks": {}, "created": time.time()}
    for check in checks:
        command = PROFILES[check]
        try:
            process = subprocess.run(command, cwd=str(worktree), shell=False,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, timeout=timeout, check=False)
            output = redact(process.stdout[-2000:].decode("utf-8", "replace"))
            report["checks"][check] = {"passed": process.returncode == 0,
                                        "exit_code": process.returncode,
                                        "output_tail": output}
        except subprocess.TimeoutExpired:
            report["checks"][check] = {"passed": False, "exit_code": 124,
                                        "output_tail": "check timed out"}
    after = checkout_fingerprint(worktree, task["base_commit"])
    report["checkout_after"] = after
    report["unchanged"] = before == after
    report["passed"] = report["unchanged"] and all(
        c["passed"] for c in report["checks"].values())
    artifact = Path(cfg["artifact_root"]).expanduser().resolve(strict=True) / task_id / "quality.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.exists():
        raise ValueError("quality receipt already exists; retain history and create a new task for reruns")
    data = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    artifact.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    store.add_artifact(task_id, "operator", "engineering_quality", str(artifact), digest)
    # This ledger is deliberately NOT exposed through MCP. Registered model
    # artifacts (even forged producer='operator') cannot satisfy this gate.
    store.db.execute("""INSERT INTO flow_quality_receipts
        (task_id,digest,artifact_path,recorded_at) VALUES(?,?,?,?)""",
        (task_id, digest, str(artifact), time.time()))
    return report


def assert_quality(store, task_id, required):
    """Operator acceptance gate checks receipt bytes and CURRENT checkout."""
    if not required:
        return
    if not isinstance(required, list) or any(c not in PROFILES for c in required):
        raise ValueError("invalid stored required checks")
    grant = store.db.execute(
        "SELECT digest,artifact_path FROM flow_quality_receipts WHERE task_id=?",
        (task_id,)).fetchone()
    if grant is None:
        raise ValueError("missing operator-origin engineering quality receipt")
    refs = [a for a in store.artifacts(task_id) if a["kind"] == "engineering_quality"
            and a["producer"] == "operator" and a["digest"] == grant["digest"]
            and a["path"] == grant["artifact_path"]]
    if len(refs) != 1:
        raise ValueError("missing or ambiguous local engineering quality receipt")
    record = refs[0]
    path = Path(record["path"]).resolve(strict=True)
    if path.is_symlink() or path.stat().st_size > 50_000:
        raise ValueError("unsafe local quality receipt")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["digest"]:
        raise ValueError("local quality receipt digest mismatch")
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate quality JSON key")
            out[k] = v
        return out
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    task = store.task(task_id)
    if data.get("task_id") != task_id or data.get("base_commit") != task["base_commit"]:
        raise ValueError("quality receipt task pin mismatch")
    if data.get("passed") is not True or data.get("unchanged") is not True:
        raise ValueError("required quality checks have not passed")
    if not all(data.get("checks", {}).get(name, {}).get("passed") is True for name in required):
        raise ValueError("missing required passing quality check")
    if not task.get("workspace"):
        raise ValueError("missing quality checkout")
    if checkout_fingerprint(task["workspace"], task["base_commit"]) != data.get("checkout_after"):
        raise ValueError("source changed since quality check")

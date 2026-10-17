"""Fail-closed source snapshot materialization for a downstream TESTER.

ONE workers each run from a pinned base commit. A test agent otherwise tests
old code. We copy only digest-verified predecessor changed files into its *own*
isolated worktree. No upstream edit, merge, clean, or git push is performed.

This is not an OS sandbox and not a substitute for independent test evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from .safety import validate_paths
from .worktrees import _git

MAX_FILE = 10_000_000
MAX_TOTAL = 30_000_000
HEX_SHA = re.compile(r"^[a-f0-9]{64}$")


def file_digest(path: Path):
    if not path.is_file() or path.is_symlink():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strict_json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=unique)


def _safe_child(root, name):
    validate_paths([name])
    if name in {".", ".."} or not name.strip() or name.startswith("./"):
        raise ValueError("ambiguous source path")
    base = Path(root).resolve(strict=True)
    current = base
    for segment in Path(name).parts:
        current = current / segment
        if current.is_symlink():
            raise ValueError("symlink in source or destination path")
    path = (base / name).resolve(strict=False)
    if not path.is_relative_to(base):
        raise ValueError("source path escapes worktree")
    return path


def _ancestors(store, task_id):
    row = store.db.execute("SELECT flow_id,step_key,role FROM flow_steps WHERE task_id=?",
                           (task_id,)).fetchone()
    if row is None or row["role"] != "tester":
        return []
    result = store.db.execute("""WITH RECURSIVE upstream(step_key) AS (
          SELECT upstream_key FROM flow_dependencies
          WHERE flow_id=? AND step_key=?
          UNION
          SELECT d.upstream_key FROM flow_dependencies d
          JOIN upstream u ON d.step_key=u.step_key
          WHERE d.flow_id=?
        )
        SELECT fs.task_id FROM upstream u JOIN flow_steps fs
          ON fs.flow_id=? AND fs.step_key=u.step_key AND fs.role='implementer'
        ORDER BY fs.step_order""",
        (row["flow_id"], row["step_key"], row["flow_id"], row["flow_id"]))
    return [store.task(r["task_id"]) for r in result]


def _read_receipt(store, prior, artifact_root):
    root = Path(artifact_root).resolve(strict=True)
    receipt = (root / prior["id"] / "changes.json").resolve(strict=True)
    if not receipt.is_relative_to(root) or receipt.stat().st_size > 100000:
        raise ValueError("unsafe source receipt")
    matches = [a for a in store.artifacts(prior["id"])
               if a["kind"] == "change_manifest" and a["producer"] == prior["assignee"]
               and Path(a["path"]).resolve(strict=True) == receipt]
    data = receipt.read_bytes()
    if len(matches) != 1 or hashlib.sha256(data).hexdigest() != matches[0]["digest"]:
        raise ValueError("source receipt digest mismatch")
    manifest = _strict_json(data.decode("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("outside_allowlist"):
        raise ValueError("unsafe predecessor diff")
    names, digests = manifest.get("changed_paths"), manifest.get("file_sha256")
    if (not isinstance(names, list) or not isinstance(digests, dict)
            or len(names) > 100 or len(set(names)) != len(names)):
        raise ValueError("invalid source receipt schema")
    if any(not isinstance(p, str) for p in names):
        raise ValueError("invalid source path")
    if any(not isinstance(k, str) or k not in names or
           not isinstance(v, str) or not HEX_SHA.fullmatch(v)
           for k, v in digests.items()):
        raise ValueError("invalid source digest mapping")
    return names, digests


def materialize_for_tester(store, task, destination, artifact_root):
    """Return borrowed-path expected hashes (None means predecessor deletion).

    A zero-edit implementation still counts as a valid source receipt.
    Multiple upstream implementations require explicit integration; never
    silently pick one or compose conflicting patches.
    """
    ancestors = _ancestors(store, task["id"])
    if not ancestors:
        return {}
    if len(ancestors) != 1:
        raise ValueError("ambiguous predecessor implementations: integrate explicitly before testing")
    prior = ancestors[0]
    if prior["status"] != "completed" or prior.get("base_commit") != task["base_commit"]:
        raise ValueError("predecessor not completed at identical base commit")
    workspace = Path(prior.get("workspace") or "").expanduser().resolve(strict=True)
    dest = Path(destination).expanduser().resolve(strict=True)
    if workspace == dest:
        raise ValueError("test stage cannot modify predecessor worktree")
    original = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    current = _git(dest, "rev-parse", "HEAD").stdout.strip()
    if current != task["base_commit"] or original != current:
        raise ValueError("worktree commit mismatch")
    names, digests = _read_receipt(store, prior, artifact_root)

    # Detect concurrent predecessor modifications and unsupported Git operations.
    # Renames/copies have two paths in porcelain; refuse rather than leave an
    # old path behind in the test snapshot.
    from .worker import _changes
    status = _git(workspace, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    if any(line[:2].strip() in {"R", "C"} or
           ("R" in line[:2] or "C" in line[:2]) for line in status):
        raise ValueError("rename/copy requires explicit patch integration")
    actual_changes = set(_changes(workspace))
    if actual_changes != set(names):
        raise ValueError("predecessor changed since source receipt")
    if _changes(dest):
        raise ValueError("test destination must be clean before snapshot materialization")

    planned, cumulative = [], 0
    for name in names:
        src = _safe_child(workspace, name)
        dst = _safe_child(dest, name)
        digest = digests.get(name)
        if digest is None:
            if src.exists() or src.is_symlink():
                raise ValueError("deletion receipt inconsistent with source")
            planned.append((name, None, dst, None))
            continue
        if not src.is_file() or src.is_symlink() or src.stat().st_size > MAX_FILE:
            raise ValueError("predecessor file missing, unsafe, or oversized")
        cumulative += src.stat().st_size
        if cumulative > MAX_TOTAL or file_digest(src) != digest:
            raise ValueError("predecessor source hash or materialization budget mismatch")
        if dst.exists() and (not dst.is_file() or dst.is_symlink()):
            raise ValueError("test destination is not regular file")
        planned.append((name, src, dst, digest))
    # Complete validation precedes the first copy to avoid partial unsafe overlays.
    for name, src, dst, digest in planned:
        if src is None:
            if dst.exists():
                dst.unlink()
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            _safe_child(dest, name)  # re-check any directory components
            shutil.copyfile(src, dst)
            os.chmod(dst, stat.S_IMODE(src.stat().st_mode) & ~0o6000)
            if file_digest(dst) != digest:
                raise ValueError("copy digest mismatch")
    return {name: digest for name, src, dst, digest in planned}


def classify_test_changes(destination, borrowed, changed):
    """Exclude unchanged borrowed patch files; reject test-writes to the patch."""
    owned, illegal = [], []
    for name in changed:
        if name not in borrowed:
            owned.append(name)
            continue
        p = _safe_child(destination, name)
        actual = file_digest(p)
        if actual != borrowed[name]:
            illegal.append(name)
            owned.append(name)
    # If the agent restored a borrowed deletion it appears in changed paths
    # even though it wasn't part of the initial overlay.
    for name, expected in borrowed.items():
        if name not in changed and file_digest(_safe_child(destination, name)) != expected:
            illegal.append(name)
            owned.append(name)
    return sorted(set(owned)), sorted(set(illegal))

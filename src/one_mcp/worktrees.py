"""Explicit git worktree isolation; refuses unpinned or unclean source roots."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _git(repo: Path, *args, timeout=30):
    return subprocess.run(["git", "-C", str(repo), *args],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, timeout=timeout,
                          text=True)


def isolate(repo_path: str, worktree_root: str, task: dict) -> Path:
    repo = Path(repo_path).expanduser().resolve(strict=True)
    root = Path(worktree_root).expanduser().resolve(strict=True)
    if not repo.is_dir() or not root.is_dir():
        raise ValueError("repository and worktree root must exist")
    commit = task.get("base_commit", "")
    if not re.fullmatch("[a-fA-F0-9]{40}", commit):
        raise ValueError("exact 40-character base commit required for execution")
    if root == repo or repo in root.parents:
        raise ValueError("worktree root must be outside repository")
    task_id = task["id"]
    if not re.fullmatch(r"TASK-[a-f0-9]{16}", task_id):
        raise ValueError("invalid task ID")
    actual = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual != repo:
        raise ValueError("repository must be a canonical git root")
    target = root / task_id
    if target.exists():
        raise ValueError("worktree already exists; inspect before retry")
    # This does not modify or reset the operator's existing checked-out files.
    _git(repo, "cat-file", "-e", commit + "^{commit}")
    _git(repo, "worktree", "add", "--detach", str(target), commit, timeout=90)
    return target

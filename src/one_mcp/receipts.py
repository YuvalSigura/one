"""Bounded, deterministic file/line receipts for role-to-role handoffs.

Only worker-observed diffs are file anchors. Model summaries are labelled untrusted.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from .safety import redact

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _diff_hunks(workspace, changed):
    """Use zero-context unified diffs; never place potentially huge diffs in model context."""
    hints = {}
    try:
        with tempfile.TemporaryFile() as output:
            subprocess.run(["git", "-C", str(workspace), "diff", "--no-ext-diff", "--no-color",
                            "--unified=0", "HEAD", "--"], stdout=output,
                           stderr=subprocess.DEVNULL, check=True, timeout=30)
            output.seek(0)
            text = output.read(250_000).decode("utf-8", "replace")
        current = None
        for line in text.splitlines():
            if line.startswith("+++ b/"):
                candidate = line[6:]
                current = candidate if candidate in changed else None
            elif current:
                match = HUNK.match(line)
                if match:
                    start, count = int(match[1]), int(match[2] or "1")
                    end = start + max(0, count - 1)
                    if count == 0:
                        end = start
                    hints.setdefault(current, []).append(f"L{start}-L{end}")
    except (subprocess.SubprocessError, OSError):
        pass
    return hints


def _short_handoff(stdout_path):
    """Opt-in one-line role packet. Do not forward complete transcripts."""
    try:
        with Path(stdout_path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 20000))
            tail = stream.read().decode("utf-8", "replace")
        lines = [line.split("ONE_HANDOFF_JSON:", 1)[1].strip()
                 for line in tail.splitlines() if line.startswith("ONE_HANDOFF_JSON:")]
        if not lines:
            return None
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate JSON keys")
                obj[key] = value
            return obj
        packet = json.loads(lines[-1], object_pairs_hook=unique)
        if not isinstance(packet, dict) or set(packet) - {"summary", "tests", "risks", "next_focus"}:
            return None
        cleaned = {}
        for key, value in packet.items():
            if key == "summary" and isinstance(value, str):
                cleaned[key] = redact(value[:600])
            elif key in {"tests", "risks", "next_focus"} and isinstance(value, list):
                cleaned[key] = [redact(s[:180]) for s in value[:5] if isinstance(s, str)]
        return cleaned
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def make_manifest(workspace, changed_paths, outside_allowlist, stdout_path):
    """Produce precise locations without reading or shipping source contents."""
    root = Path(workspace).resolve(strict=True)
    changed = set(changed_paths)
    diffs = _diff_hunks(root, changed)
    anchors, digests = [], {}
    for filename in changed_paths[:100]:
        if not isinstance(filename, str):
            continue
        loc = (root / filename).resolve(strict=False)
        if not loc.is_relative_to(root):
            continue
        if loc.is_file() and not loc.is_symlink() and loc.stat().st_size <= 10_000_000:
            try:
                digests[filename] = _hash_file(loc)
            except OSError:
                pass
        ranges = diffs.get(filename, [])[:10]
        if not ranges and loc.is_file() and not loc.is_symlink():
            # New/untracked text file: give a bounded *starting range*, never
            # claim exact coverage of a file that may be enormous.
            try:
                with loc.open("rb") as stream:
                    sample = stream.read(65536)
                if b"\x00" not in sample:
                    total = sample.count(b"\n") + (0 if not sample or sample.endswith(b"\n") else 1)
                    ranges = [f"L1-L{max(1, min(150, total))} (sample/partial)"]
            except OSError:
                pass
        if ranges:
            anchors.append({"path": filename, "lines": ranges})
    return {"changed_paths": changed_paths, "outside_allowlist": outside_allowlist,
            "line_hints": anchors, "file_sha256": digests,
            "handoff_summary": _short_handoff(stdout_path)}

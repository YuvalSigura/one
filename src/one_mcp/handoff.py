"""Verify predecessor source snapshots and build bounded, untrusted line excerpts.

Receipts are location hints, not proof of correctness. This module prevents a
silent handoff from relying on changed/stale predecessor worktree contents.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .safety import redact, validate_paths

HINT = re.compile(r"^L([1-9]\d*)-L([1-9]\d*)")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def focused_predecessor(workspace, manifest):
    """Return verified, very short file snippets without reading entire repositories.

    Missing/mismatched receipt hashes, unsafe paths and symlink escapes fail
    closed. Large or nontext files are represented by file:line references only.
    """
    root = Path(workspace).expanduser().resolve(strict=True)
    changed = manifest["changed_paths"]
    hashes = manifest.get("file_sha256")
    hints = manifest.get("line_hints", [])
    if (not isinstance(changed, list) or not isinstance(hashes, dict)
            or not isinstance(hints, list) or len(changed) > 200):
        raise ValueError("invalid predecessor receipt schema")
    if manifest.get("outside_allowlist"):
        raise ValueError("predecessor modified paths outside approved scope")
    lines = []
    for name in changed:
        if not isinstance(name, str):
            raise ValueError("invalid predecessor path")
        # Validate each path individually (ONE's normal limit is 50 paths per task).
        validate_paths([name])
        p = (root / name).resolve(strict=False)
        if not p.is_relative_to(root):
            raise ValueError("predecessor path escapes worktree")
        expected = hashes.get(name)
        if p.is_file():
            if p.is_symlink() or p.stat().st_size > 10_000_000:
                if expected:
                    raise ValueError("predecessor hash cannot be verified")
                continue
            if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise ValueError("predecessor file missing SHA-256 receipt")
            if _sha256(p) != expected:
                raise ValueError("predecessor file modified since completion")
        elif expected:
            raise ValueError("previously hashed predecessor file missing")

    budget = 2000
    for hint in hints[:12]:
        if budget < 60:
            break
        if not isinstance(hint, dict) or hint.get("path") not in changed:
            continue
        name = hint["path"]
        if name not in hashes:
            continue
        path = (root / name).resolve(strict=True)
        if path.stat().st_size > 128_000:
            continue
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != hashes[name]:
            raise ValueError("predecessor changed during excerpt capture")
        if b"\x00" in body:
            continue
        text_lines = body.decode("utf-8", "replace").splitlines()
        ranges = hint.get("lines", [])
        if not isinstance(ranges, list):
            continue
        for raw in ranges[:3]:
            if not isinstance(raw, str):
                continue
            match = HINT.match(raw)
            if not match:
                continue
            start, end = int(match[1]), int(match[2])
            if end < start or start > len(text_lines):
                continue
            # Maximum 6 source lines per range. Too large a change becomes a
            # pointer for the reviewer to inspect, not a full transcript.
            end = min(len(text_lines), end, start + 5)
            excerpt = "\n".join(f"  L{i}: {text_lines[i - 1][:160]}"
                                for i in range(start, end + 1))
            safe = redact(excerpt).replace("\x1b", "")
            value = f"UNTRUSTED source excerpt {name}:{raw}\n{safe}"
            value = value[:budget]
            lines.append(value)
            budget -= len(value)
    if not lines:
        lines.append("No safe short source excerpts available; use the supplied file:line pointers.")
    return "\n".join(lines)

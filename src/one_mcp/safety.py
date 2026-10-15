"""Bounded, redacted model-facing text; no secrets or arbitrary file retrieval."""
from __future__ import annotations

import re
from pathlib import Path

MAX_GOAL = 8000
MAX_MESSAGE = 6000
MAX_RESULT = 4000
MAX_TITLE = 200
MAX_ARTIFACT_PATH = 2048

# These patterns reduce accidental leakage; they are NOT a substitute for a DLP boundary.
_PATTERNS = (
    re.compile(r"(?i)(\b(?:api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*)[^\s,;\]\}]+"),
    re.compile(r"(?i)\bBearer\s+[a-z0-9_\.\-~+/]+=*"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


def bounded(value: str, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters")
    return value.strip()


def redact(value: str) -> str:
    text = str(value)
    for pattern in _PATTERNS:
        if pattern.pattern.startswith("(?i)("):
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def validate_paths(paths: list[str]) -> list[str]:
    if not isinstance(paths, list) or len(paths) > 50:
        raise ValueError("allowed_paths must be a list of <= 50 relative paths")
    clean = []
    for path in paths:
        if not isinstance(path, str) or not path or len(path) > 250 or "\\" in path:
            raise ValueError("paths must be nonempty slash-separated relative paths")
        pp = Path(path)
        if pp.is_absolute() or ".." in pp.parts or path.startswith("/") or ":" in path or "\x00" in path:
            raise ValueError("absolute, parent, or drive paths are not permitted")
        segments = [segment.lower() for segment in pp.parts]
        if any(segment in {".git", ".runtime", ".one", ".env", "secrets", "credentials"}
               or segment.startswith(".env.") for segment in segments):
            raise ValueError("protected runtime, git, env, or secret path")
        clean.append(path)
    return clean


def ensure_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("digest must be lowercase sha256")
    return value

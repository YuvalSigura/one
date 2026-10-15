"""Emit verifiable client connection snippets without mutating user configuration."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .store import state_path


def configurations(python_path: str | None = None, db_path: str | None = None) -> dict[str, str]:
    """Generate stdio configurations for supported local CLI clients."""
    python_path = str(Path(python_path or sys.executable).resolve())
    db_path = str(Path(db_path or state_path()).expanduser().resolve())
    command = python_path
    args = ["-m", "one_mcp.server"]
    # JSON double-quoted strings are legal TOML strings and correctly escape Windows paths.
    def quoted(value):
        return json.dumps(value, ensure_ascii=False)
    def toml():
        return (f"command = {quoted(command)}\n"
                f"args = {quoted(args)}\n"
                f"env = {{ ONE_DB = {quoted(db_path)} }}\n")
    claude = json.dumps({"mcpServers": {"one": {"command": command, "args": args,
                                                   "env": {"ONE_DB": db_path}}}}, indent=2)
    grok = "[mcp_servers.one]\n" + toml() + "enabled = true\n"
    codex = "[mcp_servers.one]\n" + toml()
    return {"claude.mcp.json": claude + "\n", "grok.config.toml": grok,
            "codex.config.toml": codex}


def emit(output_dir: str | Path | None = None) -> dict:
    data = configurations()
    if output_dir is None:
        return {"files": data, "note": "Copy into trusted client configuration; nothing was modified."}
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for name, contents in data.items():
        target = root / name
        if target.exists():
            raise FileExistsError(f"refusing to overwrite {target}")
        target.write_text(contents, encoding="utf-8")
        outputs[name] = str(target)
    return {"written": outputs, "note": "Templates only; user client configuration unchanged."}

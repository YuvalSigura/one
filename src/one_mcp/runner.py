"""Explicit operator-side CLI adapter. NOT exposed as an MCP tool.

Operator supplies reviewed argv in local config; no shell interpolation. This bridge
does not assume undocumented CLI flags and never launches processes from MCP input.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from .store import Store

ALLOWED = {"claude","codex","grok-cli"}
def run(agent, config_path, execute=False):
    if agent not in ALLOWED:
        raise ValueError("unsupported agent")
    cfg=json.loads(Path(config_path).read_text(encoding="utf-8"))
    row=cfg.get("agents",{}).get(agent)
    if not isinstance(row,dict):
        raise ValueError("agent missing in local config")
    argv=row.get("argv")
    if not isinstance(argv,list) or not argv or not all(isinstance(a,str) and a for a in argv):
        raise ValueError("argv must be a nonempty string array")
    # No arbitrary tool/command: first executable must be the reviewed configured agent.
    exe=Path(argv[0]).name.lower()
    approved={"claude":{"claude","claude.exe","claude.cmd"},
              "codex":{"codex","codex.exe","codex.cmd"},
              "grok-cli":{"grok","grok.exe","grok.cmd","grok-cli","grok-cli.exe","grok-cli.cmd"}}
    if exe not in approved[agent]:
        raise ValueError("executable does not match selected agent")
    cwd=Path(row.get("cwd") or os.getcwd()).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    if not execute:
        return {"status":"dry_run","agent":agent,"argv":argv,"cwd":str(cwd)}
    timeout=min(max(int(row.get("timeout_seconds",300)),1),1800)
    # No shell, no inherited stdin, no implicit target access, bounded runtime/output.
    proc=subprocess.run(argv,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,timeout=timeout,check=False)
    return {"status":"exited","agent":agent,"exit_code":proc.returncode,
            "stdout_tail":proc.stdout[-12000:].decode("utf-8","replace"),
            "stderr_tail":proc.stderr[-4000:].decode("utf-8","replace"),
            "truncated":len(proc.stdout)>12000 or len(proc.stderr)>4000}

def main():
    p=argparse.ArgumentParser(description="Operator-approved ONE CLI adapter; not MCP-accessible")
    p.add_argument("agent",choices=sorted(ALLOWED))
    p.add_argument("--config",required=True,help="Local untracked JSON file")
    p.add_argument("--execute",action="store_true",help="Explicitly launch reviewed CLI argv")
    a=p.parse_args()
    try:
        result=run(a.agent,a.config,a.execute)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status":"error","error":str(exc)}),file=sys.stderr)
        raise SystemExit(1)

if __name__=="__main__":
    main()

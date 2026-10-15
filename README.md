# ONE — Local MCP Bridge

ONE connects **Grok Bot**, **Grok CLI**, **Claude Code** and **Codex** through a shared **local MCP coordination interface**. Grok Bot remains the development manager; ONE does not replace it or HOMELAB's research CEO.

## Current implementation (MVP)

- A real MCP **stdio server** with nine tools: task creation/list/get/claim/finish, agent messages/inboxes and SHA256 artifact references.
- Durable SQLite state shared across separate MCP client processes.
- Small operator CLI to inspect and submit tasks.
- Separate **operator-only** CLI process adapter for verifying locally installed agent commands; **not callable through MCP**.
- No external services, model calls, secrets, target-facing actions or changes to HOMELAB.

**Important:** This is a working coordination MCP server, **not yet a verified autonomous Grok Bot ↔ Claude ↔ Codex ↔ Grok CLI integration**. Local Grok Bot MCP registration and CLI execution protocols must be verified on the operator machine. MCP agent names are labels, not authenticated users.

## Install (Windows PowerShell / Python 3.11+)

```powershell
git clone https://github.com/YuvalSigura/one.git
cd one
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:ONE_DB = "$HOME\.one\state.db"
.\.venv\Scripts\python.exe -m one_mcp.server
```

The last command starts a **stdio MCP server** and waits for an MCP client. It is normal for the terminal to show nothing. Do not use it as an interactive chat.

### MCP client registration

Use the client's own verified MCP configuration interface. Generic stdio server settings:

```json
{
  "command": "C:\\path\\to\\one\\.venv\\Scripts\\python.exe",
  "args": ["-m", "one_mcp.server"],
  "env": {"ONE_DB": "C:\\Users\\YOUR_USER\\.one\\state.db"}
}
```

Use the **same ONE_DB absolute path** for each local client. The CLI may be used independently:

```powershell
one create "P0-1 review" "Review evidence-backed differential outcome patch" codex --base-commit HEAD --acceptance "offline regressions pass"
one list
one inbox claude
```

### Local CLI adapter (separate explicit approval)

```powershell
# Copy config/agents.example.json to a local untracked file, edit executable/cwd.
py -m one_mcp.runner codex --config config/agents.example.json
# After reviewing argv, operator may explicitly launch:
py -m one_mcp.runner codex --config config/agents.example.json --execute
```

The example runs only `--version`. ONE does **not** automatically invoke any paid model. The runner cannot be called by MCP tools.

## Tests

```powershell
py -m pip install -e .
py -m unittest discover -s tests -v
```

## Safety and limitations

- Do not store credentials or target secrets in task descriptions/messages.
- Do not expose the MCP stdio server over the internet.
- MCP client-supplied agent labels are **not authentication**.
- Budgets are metadata, not enforced quota limits.
- This version does not launch other CLIs from Grok Bot or auto-create worktrees.
- See [architecture and next gates](docs/ARCHITECTURE.md).

This repository is intentionally separate from `YuvalSigura/harness`.

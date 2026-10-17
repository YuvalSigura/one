# ONE — Local multi-agent MCP bridge

**Goal:** connect your existing Grok Bot development manager with **Grok CLI, Claude Code and Codex** through one shared, persistent, local coordination interface. ONE does **not** replace Grok Bot and does **not** control HOMELAB investigations.

## What is built (v0.2)

- Real **stdio MCP** server with 9 tools for tasks, messages/inbox acknowledgements, artifact references and task events.
- Shared **SQLite WAL** state, idempotent dispatch keys, atomic claims, explicit approval, durable events and crash-safe failure-to-retry behavior.
- Operator-controlled **CLI workers** for Codex, Claude and Grok CLI. Executables and arguments are locally reviewed; worker launches **one approved task**, with a timeout, in a fresh pinned **Git worktree**, collects logs and path-change receipts, and reports its completion to Grok Bot's inbox.
- Windows-friendly connection-config generator for Grok CLI, Claude Code and Codex. It never edits existing provider settings automatically.
- Offline tests and CI workflow. No API keys or cloud services required for the **MCP server** itself.

**Boundaries:** MCP is a coordination channel, not an OS security boundary. It does not grant target access, does not launch models, and cannot bypass the operator's approval. Model invocations are possible **only** through the separately invoked operator worker. The existing Grok Bot application's direct attachment interface has not been tested on your PC; end-to-end four-way operation is not yet claimed.

## 1. Install on Windows (PowerShell)

```powershell
git clone https://github.com/YuvalSigura/one.git
cd one
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

No background service needs to be installed. Each MCP host starts `python -m one_mcp.server` as its own stdio child; they share the same SQLite database path.

## 2. Connect the three local CLI clients

```powershell
.\.venv\Scripts\python.exe -m one_mcp.cli connections --output-dir .one\connections
```

This writes **three templates**, not live configuration changes:

| File | Place the contents in |
|---|---|
| `claude.mcp.json` | Project `.mcp.json` for Claude Code (merge with existing config; don't overwrite) |
| `grok.config.toml` | Project `.grok/config.toml` for Grok CLI (merge `[mcp_servers.one]`) |
| `codex.config.toml` | Codex `~/.codex/config.toml` (merge `[mcp_servers.one]`) |

All templates use the same absolute `ONE_DB` and the Python interpreter used to generate them. If you invoke `one connections` under a different Python environment, check paths first. Avoid merging config files automatically because each client may already have MCP servers configured.

**CLI validation:** Grok CLI: `grok mcp doctor one`; Claude Code: `claude mcp list`; Codex: `codex mcp list`. The installed versions determine exact behavior. See `docs/CONNECT_GROK_BOT.md` for the separate Grok Bot connection check.

## 3. MCP interface / test message

Use `one_create_task` from one of your connected MCP clients. Each task has `title`, `goal`, `assignee`, `request_key`, optional pinned `base_commit`, `allowed_paths` and `acceptance`. Use `one_inbox` and `one_send_message` to communicate. Tools only persist metadata; **they do not execute CLIs or approve tasks**.

The equivalent local commands are:

```powershell
.\.venv\Scripts\python.exe -m one_mcp.cli create "Review P0-1" "Review scientific-feedback patch" codex --request-key P0-1-review
.\.venv\Scripts\python.exe -m one_mcp.cli list
.\.venv\Scripts\python.exe -m one_mcp.cli inbox grok-bot
```

## 3a. Optional outbound webhook (wake Grok Bot instead of polling)

When `ONE_WEBHOOK_URL` is set, every task **status change** (queued→running, running→completed/failed/blocked, queued→cancelled, lease expiry→blocked) and every **task message** is POSTed as JSON to that URL after the SQLite write commits. The hook lives in the store layer, so the MCP server, the CLI and the worker are all covered. Delivery is fire-and-forget (stdlib `urllib`, 3 s timeout, daemon thread, no redirects followed); failures are logged to stderr only and never affect the DB write or the MCP stdout stream.

| Variable | Meaning |
|---|---|
| `ONE_WEBHOOK_URL` | http(s) endpoint. Unset or empty = feature disabled, no behavior change. |
| `ONE_WEBHOOK_SECRET` | Sender key. Never logged or put in the payload. |
| `ONE_WEBHOOK_AUTH_HEADER` | Header name for the key, default `Authorization` (sent as `Bearer <secret>`); any other header gets the raw secret. |
| `ONE_WEBHOOK_EVENTS` | Optional comma filter: `status`, `message`, `all`, or `status:<new status>` (e.g. `status:completed,status:failed,status:blocked`). Default: status changes + messages. |

Payload fields: `event` (`task.status_changed` or `task.message`), `task_id`, `title`, `assignee`, `owner`, `status`, `old_status`/`new_status` (status events), `message` {`id`, `sender`, `recipient`, `text` (first 2000 chars), `truncated`} (message events), `timestamp` (UTC ISO 8601), `source`, `version`.

Each MCP host starts its own server process, so the variables must be visible to Claude Code / Grok CLI / Codex (set them as user environment variables and restart those clients, or add them to the client's MCP `env` block).

## 4. Explicitly approved CLI execution (optional)

First, make a local copy of `config/agents.example.json`, replacing `YOUR_USER` and reviewing the repository, worktree and artifact paths. Ensure worktree/artifact root directories **already exist**, are outside your repo, and that your provider CLI executables work. Enable **only the specific agent you intend to use**.

```powershell
Copy-Item config\agents.example.json config\agents.local.json
# Manually edit config\agents.local.json; set your repo, roots, and codex.enabled=true.
.\.venv\Scripts\python.exe -m one_mcp.cli doctor --config config\agents.local.json
.\.venv\Scripts\python.exe -m one_mcp.cli worker codex --config config\agents.local.json
```

**The worker command above is a dry-run**. To run for real, first create a task with a **40-character pinned git commit**, allowed file paths, and approve that task locally:

```powershell
.\.venv\Scripts\python.exe -m one_mcp.cli approve TASK-REPLACE_WITH_REAL_ID
.\.venv\Scripts\python.exe -m one_mcp.cli worker codex --config config\agents.local.json --execute
```

No agent can approve another agent's paid run through MCP. One `--execute` invocation handles at most one approved task; no automatic retry and no merge/push. Change logs and local output files remain in the configured `artifact_root`, outside the repository. **Exit code 0 is not proof of successful implementation.**

To inspect a crashed process' stale task state:

```powershell
.\.venv\Scripts\python.exe -m one_mcp.cli recover
```

This blocks expired jobs; it never replays a potentially billed API call.

## 5. Development / testing

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

If `ONE_WEBHOOK_URL` is set in your environment, clear it for the shell first (`Remove-Item Env:ONE_WEBHOOK_URL`) so test tasks don't wake your real Grok Bot routine; `tests/test_webhook.py` uses its own local stub.

`tests/` covers MCP framing, cross-process persistence, schema migrations, concurrent worker claims, idempotency, messaging, approval boundaries, git-worktree isolation, changed-path quarantine, bad arguments, client configuration generation, and fail-closed recovery.

## 6. Security and known limitations

- **Never** put credentials, security target secrets, live engagement artifacts or private operator details in model-visible task messages. A best-effort scrubber is not secret isolation.
- **Do not** expose this stdio toolset on a public HTTP interface. Agent names are labels, not cryptographically authenticated identities.
- **Git worktrees are not sandboxes.** A provider CLI with wide permissions can still access other local files. Use least-privilege CLI sandbox modes and appropriate OS account separation.
- **Provider budgets are not enforced token quotas.** ONE limits one local invocation/time, not the provider's billable reasoning usage.
- **MCP protocol compatibility** is implemented for commonly supported legacy revisions; full SDK version-by-version compatibility still requires live CLI checks.
- **No direct Grok Bot connection** is claimed until its actual installed interface is tested. Grok CLI MCP support is documented but is a separate program.
- No `harness` files, logs, engagements, runtime processes, or security targets are modified by ONE installation.

Further details: [architecture](docs/ARCHITECTURE.md) · [connecting Grok Bot](docs/CONNECT_GROK_BOT.md).


## Engineering Desk (experimental, opt-in)

ONE also has a development-quality workflow layer: `python -m one_mcp.engineering templates`.
It ships eight workflows covering defects, features, refactoring, research, security hardening, agent evaluations, releases, and architecture decisions. It builds dependency-gated ONE tasks, enforces operator acceptance of hashed local evidence, and records only explicitly accepted local lessons. **No automatic model invocation, approval, merge, push, or new MCP authority**. See [Engineering Desk](docs/ENGINEERING_INTELLIGENCE.md) for usage and limitations.

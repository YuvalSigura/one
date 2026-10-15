# ONE v0.2 — architecture, capabilities, trust boundaries and real-world verification

## 1. The actual integration objective

ONE is **not another HOMELAB CEO**. The existing Grok Bot remains the coordinator for **software development**. ONE is the shared, durable local MCP coordination interface through which Grok CLI, Claude Code and Codex can discover tasks, communicate and exchange work results. It is **not** a HOMELAB target-execution control plane and never binds to HOMELAB WorkGraph, ActionPlane, IdentitySessionStore or Vault.

```text
                  Existing Grok Bot (coordinator)
                            |
                [client/terminal integration TBC]
                            |
                   ONE task/MCP store
                   SQLite + stdio tools
                 /         |          \
         Grok CLI       Claude Code     Codex CLI
                 \         |          /
                     local operator worker
                    isolated Git worktrees
                    local artifact receipts
                    offline review + acceptance
```

## 2. Implemented

- MCP stdio JSON-RPC request handling (`initialize`, `ping`, `tools/list`, `tools/call`). Protocol negotiation for the legacy 2024-11-05, 2025-03-26, 2025-06-18 revisions. This custom subset does not implement every feature of the full SDK and has not been validated against every CLI version.
- A finite set of typed tools: create/list/get tasks, send/read/ack inbox messages, register/list artifact metadata, list task events.
- SQLite WAL persistence, idempotent task creation (`request_key`), atomic worker claims, bounded outputs, append-only events, v0.1 additive schema migration.
- Local operator approval **outside MCP**; no arbitrary CLI execution tool available to model-visible clients.
- One approved task per worker invocation (`one worker ... --execute`). Config is operator-owned/untracked, agent executable name checked against expected provider, flags for permission bypass rejected, timeout applied and outputs saved to local artifact files.
- Each execution creates a pinned, detached Git worktree. Unpinned commits, path policy violations and existing worktree collisions fail closed. Source repository checked-out files are never reset.
- Worker reports local log artifact hashes and changed-path manifest, but does not claim that artifact bytes prove a security outcome or that tests have passed.
- A task completion creates a message to the `grok-bot` inbox. This is **pollable**: ONE has no undocumented push API to the Grok Bot UI.
- Generates local configuration snippets for the three CLI tools; does not modify existing settings.

## 3. Explicit limits; do not misrepresent these as production guarantees

**Agent identity.** MCP stdio processes on the *same operating-system user* are in one trust domain. Sender/assignee labels are not cryptographic principal authentication; a compromised local client can impersonate another label and may invoke local operator programs if granted shell access. ONE assumes trusted local users and agents with appropriately restricted tool capabilities. For cross-boundary deployment, add authenticated, authorized identities and a documented approval path.

**Isolation.** Git worktrees isolate tracked source snapshots and competing changes; **they are not a security sandbox**. A local model CLI with broad shell, filesystem, network or credential access may still reach the original workspace. Use provider sandbox/permissions and OS account isolation as appropriate. Codex configuration example requests `--sandbox workspace-write`; Claude and Grok permissions must be configured explicitly by the operator.

**Spending.** No model calls occur through MCP. The operator worker invokes one model CLI at a time, with a wall-clock timeout. The numeric budget field is **advisory only**. It does not read model providers' quotas and cannot enforce billed-token budgets. Avoid parallel workers against the same provider without an external quota guard.

**Completion.** A CLI exit code of zero is *not* accepted code, passing tests, proof or successful autonomous research. There is no automatic merge, push, privilege escalation, security-target execution or live qualification.

**Resumption.** Task state survives server restarts. Expired worker leases become `blocked` and require human inspection, not automatic retries that might incur another bill. This is safe **at-least-once-delivery avoidance**, not transparent continuation of a model's full conversation state.

**Grok Bot.** Grok CLI supports standard MCP configuration. Grok Bot's own precise local MCP / terminal attachment interface must be confirmed using its installation. Do not claim automatic integration with Grok Bot until an actual interaction from that application successfully exercises a task tool.

**Protocol compatibility.** The pure-Python, zero-dependency server is intentionally a compact legacy MCP subset. It was tested with direct JSON-RPC stdio frames, not with installed provider CLIs. Newer MCP revisions and transports may require upgrading to the official SDK. No network listener is exposed.

## 4. Information flow

1. The coordinator creates a short task with unique request key, intended assignee, pinned git commit, allowed source paths, acceptance instructions and advisory budget.
2. All clients connected through ONE's MCP stdio tools can discover their assigned tasks; scope-bound message contents are small and secret-scrubbed.
3. The operator reviews and approves specific tasks using local CLI (`one approve TASK-...`). MCP cannot approve.
4. With an explicitly enabled local config, the operator invokes `one worker codex --config ... --execute` (or the Claude/Grok equivalent). ONE atomically claims ONE task.
5. Worker checks exact commit, makes isolated worktree, writes a bounded task brief, runs the configured provider CLI once, records outputs/changed file names, and blocks if paths changed outside the task allowlist.
6. `one_inbox` for Grok Bot receives a completion notification; Claude can be assigned a separate read/review task and send review feedback over ONE. This chain is coordinated via task creation rather than direct unbounded model recursion.
7. Human reviews the diff and test receipts and performs any merge/push separately.

## 5. Security, recovery and fault scenarios

| Fault | Behavior |
|---|---|
| Same task idempotency key replayed | Returns original task, if body identical; conflicting contents rejected |
| Multiple workers racing | SQLite `BEGIN IMMEDIATE` claims the task once |
| Model worker exits nonzero | Task marked failed and local logs registered |
| Worker crashes mid-run | Expired lease marked blocked, **not automatically rerun** |
| No approval | Worker returns idle; nothing is launched |
| No exact commit SHA | Task blocked before model launch |
| Git worktree path already exists | Task blocked, never overwritten |
| Wrong executable or `--yolo`/permission-bypass flag | Refused |
| Changed file outside allowed paths | Task blocked, isolated worktree preserved for inspection |
| Secrets in goal/messages | Common token patterns redacted; **not comprehensive DLP** |
| MCP client sends invalid schema | Tool error, no process spawn |
| CLI quota exhausted | CLI may return nonzero; inspect error locally; no retry |
| Repo fixture gives malicious instructions | Brief directs agent to ignore those instructions; **not a hard guarantee** |

## 6. Operator acceptance gates

1. Run all local unit, compatibility and simulated Git-worktree tests (`python -m unittest discover -s tests -v`).
2. Run `one connections` and verify actual CLI registration via `grok mcp doctor one`, `claude mcp list`, `codex mcp list` (installed CLI commands may vary).
3. From each installed CLI, invoke `one_list_tasks` and `one_inbox` against the **same `ONE_DB`**; confirm cross-process message and receipt persistence.
4. Verify local Grok Bot can issue the same ONE MCP call (or an approved terminal integration) before claiming its direct connection is complete.
5. Under explicit operator consent, queue a harmless test project task, approve it and start one local CLI worker with a short wall-clock timeout. Verify Git isolation, logs, diff paths and receipt.
6. Trigger a failure/timeout and verify recovery **never** incurs a second model call automatically.
7. Only then authorize a real isolated HOMELAB development task; **never** a live target investigation.

The ONE repository provides the backend and repeatable offline gates. Actual external client integration requires local configuration and observed execution on the user's PC.

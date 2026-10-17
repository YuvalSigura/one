# ONE Role Flows — low-token multi-agent engineering

This is an experimental implementation in ONE, not a replacement for HOMELAB target authorities.

## Implemented

ONE now supports frozen role-specific DAGs stored inside its existing SQLite Store. Each stage is an ordinary ONE task with a named role, pinned 40-character Git commit, exact allowed file paths, task-local context, shared project constraints, acceptance criteria and predecessor dependencies.

- Implementer, planner, tester, reviewer, security-reviewer, researcher and integrator each get short specific work instructions.
- Dependents are automatically eligible for atomic claim when completed or manually accepted predecessors satisfy every gate.
- A completed worker exit code is not VERIFIED. Operator verdicts are immutable and only available through local CLI, never MCP.
- The next worker receives a brief from the prior stage, not the whole transcript. Worker receipts contain changed file paths, zero-context diff line ranges, content SHA-256 hashes where possible, and a bounded, clearly untrusted structured conclusion.
- A local, bounded dispatcher can continue already-approved steps from X to Y and run independent provider steps concurrently.
- ONE keeps the only task execution status; there is no shadow scheduler or competing status database. Each worker retains its existing Git worktree and operator approvals.
- MCP supports safe role-flow creation and status listing, not approval or execution.

## Handoff packet

Agents receive the following instruction to end their final answer with a single-line JSON object:

    ONE_HANDOFF_JSON: {"summary":"short result","tests":["actual command: outcome"],"risks":["remaining risk"],"next_focus":["src/file.py:45-51"]}

The next role gets its own objective, role-specific rules, shared invariant brief, the previous worker's path and relevant changed file:line ranges, and this bounded untrusted summary. Do not reread unrelated repository files unless necessary to validate the exact claim.

WARNING: each worker starts at a pinned commit in its own isolated worktree. The reviewer does NOT inherit the implementer's uncommitted changes. It must inspect the prior worktree READ-ONLY using the provided focused file hints. Worktrees are not security sandboxes. Receipt hashes may be stale if somebody subsequently edits a previous worktree.

## PowerShell workflow

First customize examples/role_flow.json. Replace all zero commit values with the same genuine 40-hex base commit in the repository configured in config/agents.local.json. Review scopes, enabled workers and objectives.

Create a workflow (no paid model call):

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-create examples\role_flow.json
    .\.venv\Scripts\python.exe -m one_mcp.cli flow-status one-pilot-001

After reviewing all stages, explicitly approve the fixed plan. This alone does not launch providers:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-approve one-pilot-001 --confirm

Inspect readiness without execution:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-run one-pilot-001 --config config\agents.local.json

With provider launches separately authorized by the operator, run already-approved stages, which can hand off automatically through dependencies until a manual acceptance gate blocks progress:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-run one-pilot-001 --config config\agents.local.json --max-tasks 8 --parallel 2 --execute

Review exact independent artifacts before permitting the final integration recommendation:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-verdict TASK-REPLACE accepted --reason "I reviewed tests and diff"

Reinvoke flow-run to proceed after a manual acceptance. Rejected verdicts cannot be rewritten; create a new repair workflow instead.

## Trust boundaries and limits

- Do not expose task approval, review verdict or model execution through MCP.
- No provider budget/quota introspection. Disable exhausted providers; never auto-retry failed paid calls.
- No auto Git push, merge, reset, clean or changes to HOMELAB targets.
- No hidden always-running process: the dispatcher advances tasks only while the local flow-run command remains running.
- The shared project context supplied through MCP is task data, not a grant of elevated privileges. Hard approvals and worker path rules remain at the real action boundary.
- No claim that worker output or multi-agent consensus is equivalent to verified evidence.
- The structured summary is untrusted. Verify material claims against real diff/tests.
- Before expanding this experimental feature, compare time to reviewed patch, context reads, errors found, false positives and provider usage on isolated fixtures.

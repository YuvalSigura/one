# ONE Role Flows — low-token multi-agent engineering

This is an experimental implementation in ONE, not a replacement for HOMELAB target authorities.

## Implemented

ONE now supports frozen role-specific DAGs stored inside its existing SQLite Store. Each stage is an ordinary ONE task with a named role, pinned 40-character Git commit, exact allowed file paths, task-local context, shared project constraints, acceptance criteria and predecessor dependencies.

- Implementer, planner, tester, reviewer, security-reviewer, researcher and integrator each get short specific work instructions.
- Dependents are automatically eligible for atomic claim when completed or manually accepted predecessors satisfy every gate.
- A completed worker exit code is not VERIFIED. Operator verdicts are immutable and only available through local CLI, never MCP.
- The next worker receives a brief from the prior stage, not the whole transcript. Worker receipts contain changed file paths, zero-context diff line ranges, content SHA-256 hashes where possible, and a bounded, clearly untrusted structured conclusion.
- The operator preauthorizes the frozen pipeline once, but only dependency-ready steps become approved ONE tasks. This prevents legacy ONE v0.2 workers from claiming future steps out of order. A local, bounded dispatcher promotes newly-ready stages and runs independent providers concurrently.
- ONE keeps the only task execution status; there is no shadow scheduler or competing status database. Each worker retains its existing Git worktree and operator approvals.
- MCP supports safe role-flow creation and status listing, not approval or execution.

## How ONE decides what context the next agent receives

Each stage has a fixed role and optional context-sufficiency contract:

- "handoff_mode": "execution_only" — default for implementer/integrator. No broad re-discovery or project-wide rereads. Ask for an explicit ONE_CONTEXT_GAP if information is essential and missing.
- "handoff_mode": "focused_review" — default for reviewer, tester and security-reviewer. Independently inspect the cited diff, needed call sites and tests, but do not replay all earlier investigations.
- "handoff_mode": "research" — default for researcher/planner. Explore only the bounded research objective and permitted sources, not unrelated project history.
- "must_know": categorizes indispensable "invariants", "decisions", "facts" and "do_not_repeat" instructions. These are always prioritized over optional code excerpts. The role plan owner is responsible for supplying accurate facts.
- "required_handoff": list of mandatory predecessor evidence fields ("changed_files", "line_hints", "summary", "tests"). If a declared field is absent, a ONE_CONTEXT_GAP fails the successor's brief before launching its model.

Example for a scoped code reviewer stage:

~~~json
{
  "handoff_mode": "focused_review",
  "must_know": {
    "invariants": ["The project's existing authority model must remain unchanged"],
    "decisions": ["Fix the specified bug, not architecture"],
    "do_not_repeat": ["The root-cause discovery has already been completed"]
  },
  "required_handoff": ["changed_files", "line_hints"]
}
~~~

The handoff validates mandatory source receipt hashes and prioritizes exact file:line pointers. Optional snippets are omitted with an explicit marker if they cannot fit; **essential facts, decisions, required evidence and no-re-discovery rules are never silently truncated.** If the essentials do not fit, ONE stops with ONE_CONTEXT_GAP instead.

**Important:** A structural completeness check cannot prove that the author included every fact the next agent will need, or that an agent's claimed test success is real. The next agent must still verify key assumptions for its role. These no-re-discovery rules are instructions, not deterministic OS restrictions on reading tools. To physically forbid broad repository reads requires separately reviewed tool permission boundaries.

## Handoff packet

Agents receive the following instruction to end their final answer with a single-line JSON object:

    ONE_HANDOFF_JSON: {"summary":"short result","tests":["actual command: outcome"],"risks":["remaining risk"],"next_focus":["src/file.py:45-51"]}

The next role gets its own objective, role-specific rules, shared invariant brief, the previous worker's path and relevant changed file:line ranges, and this bounded untrusted summary. The handoff also traverses earlier ancestors (for example, implementer -> reviewer -> tester) so file locations do not vanish at an intermediate read-only review. It provides small line-numbered code excerpts after checking file SHA-256 against the worker's receipt. If a required receipt is missing, changed, or points outside the worktree, that stage blocks before invoking the provider. Do not reread unrelated repository files unless necessary to validate the exact claim.

WARNING: each worker starts at a pinned commit in its own isolated worktree. The reviewer does NOT inherit the implementer's uncommitted changes. It must inspect the prior worktree READ-ONLY using the provided focused file hints. Worktrees are not security sandboxes. Receipt hashes may be stale if somebody subsequently edits a previous worktree.

## Use one of the eight Engineering Desk templates

The existing Engineering Desk templates can feed this ONE Role Flow executor without
running its separate manual queue path. First create a reviewed Engineering Desk
plan (see docs/ENGINEERING_INTELLIGENCE.md), then:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-template C:\path\to\reviewed-engineering-plan.json
    .\.venv\Scripts\python.exe -m one_mcp.cli flow-status ecc-YOUR_PLAN_ID

The converter preserves template roles and dependencies, giving focused handoffs
between their steps. The Engineering Desk standalone manual queue and the Role Flow
automatic dispatcher are **alternative modes**. Do not enqueue the same plan through
both interfaces: that would duplicate its tasks. Only Role Flow provides the
automatic transition from an approved successful worker into the next role.

## Safely use this branch alongside Copilot/main

Do NOT switch or reset the existing ONE main checkout. Use a separate clone while
Copilot is still developing main. For the first offline tests use a distinct
ONE_DB to avoid creating pilot tasks in the shared production database:

    $pilot = "$HOME\Documents\GitHub\one-roleflow-pilot"
    git clone --branch feature/one-engineering-intelligence-20261017 --single-branch https://github.com/YuvalSigura/one.git $pilot
    cd $pilot
    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e .
    $env:ONE_DB = "$HOME\.one\roleflow-pilot.db"
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v

Run flow-create/status and dry-run commands from this pilot shell. The existing
ONE MCP server and other connected clients continue using their own previous
ONE_DB. A legacy client may claim an already-ready approved task, but no future
dependency-blocked task is approved ahead of time. Do not run legacy CLI workers
against the pilot DB during the first integration smoke test. Never silently
switch Copilot's configured ONE_DB to the pilot database.

## PowerShell workflow

First customize examples/role_flow.json. Replace all zero commit values with the same genuine 40-hex base commit in the repository configured in config/agents.local.json. Review scopes, enabled workers and objectives.

Create a workflow (no paid model call):

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-create examples\role_flow.json
    .\.venv\Scripts\python.exe -m one_mcp.cli flow-status one-pilot-001

After reviewing all stages, explicitly authorize the fixed plan. Only currently ready tasks receive individual approval; successors remain pending until the dispatcher sees their gates satisfied. This alone does not launch providers:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-approve one-pilot-001 --confirm

Inspect readiness without execution:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-run one-pilot-001 --config config\agents.local.json

With provider launches separately authorized by the operator, run already-approved stages, which can hand off automatically through dependencies until a manual acceptance gate blocks progress:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-run one-pilot-001 --config config\agents.local.json --max-tasks 8 --parallel 2 --execute

Review exact independent artifacts before permitting the final integration recommendation:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-verdict TASK-REPLACE accepted --reason "I reviewed tests and diff"

Reinvoke flow-run to proceed after a manual acceptance. Rejected verdicts cannot be rewritten; create a new repair workflow instead.

To prevent any further stages from launching after an operator changes their mind:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-revoke one-pilot-001

Revocation rejects queued task approvals and prevents later ready-stage promotion. It does NOT kill a worker already running; terminate and inspect that process separately.

## ECC-inspired improvements added in this branch

### Patch-aware tester handoffs

When a TESTER has exactly one IMPLEMENTER ancestor, ONE verifies the previous
Git worktree is still at the *same pinned base SHA*, compares its Git changes
to the registered change manifest, validates each changed file digest, and
materializes ONLY those changed files/deletions into the TESTER's new isolated
worktree before launching its CLI. The copy remains local and does not touch
the original implementation worktree or main. Only edits the TESTER actually
made after that overlay are attributed to it; any attempted modification of
borrowed source is quarantined. Unknown or multiple competing implementers,
symlinks, changed manifests, copies/renames, file budget overflow or mismatched
base commits fail closed. No automatic merging is performed.

The REVIEWER instead sees the implementer's original read-only worktree and
file:line digest-verified excerpts; it does not inherit uncommitted changes.
The REVIEWER is **blind**: the implementer's own summary and claimed test
success are withheld during the first independent review, then available to
the later integrator along with independent findings. Reviewer context still
contains the actual diff and project invariants.

### Actual deterministic engineering quality receipts

The operator can execute selected *fixed* local profiles (never model-supplied
arbitrary commands) against a **completed task's own worktree**:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-quality TASK-EXAMPLE --config config\agents.local.json --check diff
    .\.venv\Scripts\python.exe -m one_mcp.cli flow-quality TASK-EXAMPLE --config config\agents.local.json --check pytest

Profiles are \`diff\` (\`git diff --check\`), \`unittest\`
(\`python -m unittest discover -s tests -v\`) and \`pytest\` (\`python -m pytest -q\`).
Select only commands appropriate to the repository. This can execute TEST CODE
in the target checkout, so invoke it only as an explicit local operator action,
in a disposable isolated repository, with no target credentials or live
security engagement services.

Optionally declare \`"required_checks": ["diff", "pytest"]\` in a stage
JSON. When so configured, operator acceptance via \`flow-verdict ... accepted\`
is blocked until those checks have passed and the worktree fingerprint STILL
matches the checked source. Engineering results are stored as digested artifacts,
not as new execution states or as independent scientific truth. Plain
\`completed\` status is STILL NOT VERIFIED. Stages using \`gate: completed\` may
proceed without such manual acceptance; use \`gate: accepted\` where acceptance
must block successor execution.

The receipt is **single-use per task**; do not overwrite it after failed
tests. Create a fresh correction task in another worktree. No auto-reruns.

### Honest productivity reporting

Read workflow progress, operator verdicts, elapsed wall-clock, check receipts,
and actual generated brief sizes, without opening transcripts:

    .\.venv\Scripts\python.exe -m one_mcp.cli flow-metrics one-pilot-001 --config config\agents.local.json

Token savings are intentionally reported UNKNOWN until we have comparable
provider-native usage from independent matched tasks. Prompt bytes are **not**
provider tokens, and no auto model routing decisions are made from guesses.

## Trust boundaries and limits

- Do not expose task approval, review verdict or model execution through MCP.
- No provider budget/quota introspection. Disable exhausted providers; never auto-retry failed paid calls.
- No auto Git push, merge, reset, clean or changes to HOMELAB targets.
- No hidden always-running process: the dispatcher advances tasks only while the local flow-run command remains running. The first pilot must use a separate DB; the original ONE main checkout and real provider configurations remain untouched.
- The shared project context supplied through MCP is task data, not a grant of elevated privileges. Hard approvals and worker path rules remain at the real action boundary.
- No claim that worker output or multi-agent consensus is equivalent to verified evidence.
- The structured summary is untrusted. Verify material claims against real diff/tests.
- Before expanding this experimental feature, compare time to reviewed patch, context reads, errors found, false positives and provider usage on isolated fixtures.

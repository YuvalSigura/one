# ONE Engineering Desk — opt-in development intelligence (experimental)

ONE Engineering Desk adds **eight structured, ECC-inspired workflows** above the existing ONE task store. No external ECC code, plugins, models, hooks, network servers, cloud accounts or extra Python dependencies are installed. No changes to `one_mcp.server` or its nine MCP tools.

The emphasis is *shipping actual work* with a reproducible sequence: reproduce → fail-old regression → small fix → verify → independent review → explicit acceptance. Different workflows support feature delivery, trust-boundary hardening, refactoring, research falsification, agent evaluation, release review and multi-perspective architecture decisions.

## Quick start (Windows PowerShell)

All commands below use the **ONE virtual environment**. The plan is written to an operator-chosen location and **never overwrites** an existing file.

```powershell
$py = "C:\Users\yuval\Documents\GitHub\one\.venv\Scripts\python.exe"
& $py -m one_mcp.engineering templates
& $py -m one_mcp.engineering plan --id rdl-strict-json --workflow defect `
  --objective "Fix RDL RB-02 duplicate JSON key validation" `
  --repository "C:\Users\yuval\Documents\GitHub\Research-Discovery-Lab" `
  --base-commit REPLACE_WITH_ACTUAL_40_CHARACTER_SHA --allowed-path tools `
  --allowed-path tests --out "C:\Users\yuval\.one\plans\rdl-strict-json.json"
& $py -m one_mcp.engineering queue --plan "C:\Users\yuval\.one\plans\rdl-strict-json.json"
```

`queue` without `--apply` is a **read-only preview**. Add `--apply` to create only dependency-ready **unapproved, queued** ONE tasks. The normal `one approve TASK-...` and separately launched `one worker AGENT ... --execute` controls still apply. **This extension never runs providers or changes repository files.**

After an agent finishes, verify that the task is `completed` and a local evidence file is registered as an artifact of the stage's required kind. Then **the operator** can accept the exact bytes:

```powershell
& $py -m one_mcp.engineering accept --plan "C:\Users\yuval\.one\plans\rdl-strict-json.json" `
  --stage reproduce --artifact ART-REPLACE_WITH_REAL_ID --file "C:\path\to\reproduction-report.txt" `
  --note "Reviewed source, reproduction and negative cases"
& $py -m one_mcp.engineering queue --plan "C:\Users\yuval\.one\plans\rdl-strict-json.json" --apply
& $py -m one_mcp.engineering status --plan "C:\Users\yuval\.one\plans\rdl-strict-json.json"
```

Acceptance checks task state, evidence type, and the SHA-256 of the **actual local file** against ONE's registered artifact digest. It creates a durable `engineering_acceptances` record **inside the existing ONE SQLite database**. It never changes `tasks.status` and does not imply scientific, security or independent proof. Nothing may proceed on `completed` alone.

A reviewed, accepted lesson can be captured **locally** using `learn --plan ... --stage ... --lesson ... --out C:\path\to\lessons.jsonl`. Lessons are marked `project-local-unreviewed` and never promoted into global instructions automatically.

## Included workflows

| Workflow | Purpose |
| --- | --- |
| `defect` | Reproduce, red test, repair, verify, review |
| `feature` | Contract first, tests, build, verify, review |
| `security-hardening` | Trust map, negative tests, patch, fault injection, hostile review |
| `research` | Hypotheses, source provenance, falsification, independent review |
| `refactor` | Baseline, change, regression, review |
| `agent-evaluation` | Design, baseline, candidate evaluation, blind critique |
| `release` | Inventory, tests, security and release review |
| `architecture-decision` | Options, independent skeptic and pragmatic tracks, synthesis |

Assignees default to installed ONE labels (Claude/Codex/Antigravity) as a **proposal only**, regardless of provider availability. Use `--assign ROLE=AGENT` to override. No availability or token quota is assumed. Grok CLI is not a default. If an agent is unavailable, leave its work blocked/unapproved rather than invoking a different model silently.

## Responsibilities and constraints

- **ONE `tasks.status` remains the single task-execution lifecycle authority**; the new table contains local operator acceptance of one evidence artifact, not execution state or security proof.
- **Existing approvals remain binding**. No MCP approval/execution tool is added, and no model launches are performed by these commands.
- **Dependencies** are resolved from durable ONE task IDs via deterministic request keys, and each completed prerequisite needs a local operator acceptance. The plan is declarative and its SHA-256 is bound to its identifier in the ONE database on first queue; silent mid-run plan edits are rejected. ONE has no background scheduler, worker retry, automatic merging, or persistent Copilot session resume.
- **Provider-specific roles** improve task briefs, but the same model may play multiple roles. The reviewer is not magically independent or correct. Do not present a council's vote as proof.
- **Acceptance** means the local operator approved an artifact's bytes and kind; it does not prove the source is trustworthy or that the assertions are true. Do not use this as HOMELAB ProofLifecycle or RDL principle authority.
- **No global learning without explicit review**. Lessons are stored in a project-selected path and cannot silently become system rules.
- **MCP clients stay untrusted**. Worker paths are worktree-scoped but Git worktrees are not OS sandboxes. The existing ONE operator must control permissions and secrets.

## Why not import ECC directly?

ECC (https://github.com/affaan-m/ECC, MIT) is a useful inspiration for role contracts, verification loops, anti-anchoring reviews, and continuous learning. Installing its full catalog or hook system into ONE would create unnecessary configuration collisions and potential competing orchestration state. This is a small original standard-library implementation with explicit operator gates and narrow integration points.

## Development checks

```powershell
& $py -m unittest discover -s tests -v
```

Known limits: no permanent scheduling daemon, no quota enforcement, no orchestration authority over HOMELAB, no automated review of content quality, and no recovery of unverified tasks. Those are not silently inferred from a successful worker exit code.

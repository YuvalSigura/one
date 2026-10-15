# ONE — local MCP bridge architecture

## Ownership and threat boundaries

ONE is a **development coordination** bridge, not a HOMELAB target-investigation runtime. Grok Bot is the operator's existing manager. The bridge does not create a replacement CEO or use HOMELAB WorkGraph as a developer task queue.

- SQLite stores task/message/artifact **metadata** only; it is not a proof store.
- MCP stdio exposes coordination tools only. No shell, model invocation, HTTP, secrets, git push, or target-execution tool exists.
- Local MCP clients run under the **same OS user trust boundary**. The `agent` strings are **claimed labels, not authenticated identities**. Therefore role checks are workflow hygiene, not a security boundary against a malicious local MCP client. Do not attach ONE to untrusted clients or expose it to a network.
- A client may read metadata from any task; do not put secrets or sensitive target data in tasks/messages.
- CLI adapter execution is a separate, operator-invoked process and cannot be initiated through MCP.
- No automatic model calls or spending. The budget field is advisory until a verified quota-aware runner is implemented.
- Filesystem paths in artifact records are references, not automatically dereferenced or trusted.
- Use explicit Git worktrees for concurrent code changes. ONE never mutates HOMELAB.

## MCP wire protocol

Python stdlib, newline-delimited JSON-RPC 2.0 over stdio; protocolVersion 2024-11-05. Exposes initialize, ping, tools/list, tools/call. Each tool call returns an MCP text content block. No HTTP listener.

## Supported tools

`one_create_task`, `one_list_tasks`, `one_get_task`, `one_claim_task`, `one_finish_task`, `one_send_message`, `one_inbox`, `one_register_artifact`, `one_list_artifacts`.

Task lifecycle: queued → claimed → completed|failed|blocked. A task is never auto-claimed, requeued, dispatched or marked verified. Artifacts carry path and SHA256 for downstream verification, but the hub does not claim to have verified the underlying bytes.

## Connection contract

Register the same executable and `ONE_DB` path in each *verified* MCP-capable client. The clients share SQLite state, not a shared model conversation. Grok Bot must be confirmed to support local MCP server registration; Grok CLI compatibility does not imply Grok Bot compatibility. If the Bot has no local MCP interface, a supported extension/adapter is required before declaring end-to-end connectivity.

## Next implementation gate

1. Verify exact Grok Bot local integration interface and installed Claude/Codex/Grok CLI commands.
2. Add adapters that fetch assigned tasks and deliver minimal context, with operator-approved model invocation.
3. Authenticate principals if clients span different trust boundaries.
4. Add lease/recovery, idempotent request IDs, redaction, cost accounting, Git-worktree isolation and CI gates.
5. Demonstrate one real Grok Bot → Codex → Claude review → Codex correction → operator acceptance cycle with measured usage.

No claim of a complete automated cross-CLI workflow is made at this stage.

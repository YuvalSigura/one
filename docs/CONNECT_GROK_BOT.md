# Connect the existing Grok Bot (not a replacement bot)

The operator already has a Grok Bot manager with HOMELAB specialist bots. ONE supplies development-task coordination only. It does not provision new Bot accounts or copy the Bot's private state.

## Connection determination

1. Open the locally installed Grok Bot interface and inspect its integrations/terminal/MCP tool settings.
2. If a local **stdio MCP** server can be registered, use the ONE command/args/ONE_DB environment shown by `one connections`.
3. If Grok Bot can execute **local terminal commands** but cannot mount custom MCP servers, it may use `one list`, `one create`, `one inbox grok-bot` as its restricted coordination channel. This is a CLI integration, **not** a direct MCP attachment.
4. If the Bot provides neither local MCP attachment nor terminal/plugin access, do **not** invent an API or expose ONE on the internet. Obtain a supported integration mechanism from the product documentation/operator.
5. Perform a witnessed test: the Bot creates task `ONE-CONNECTIVITY-TEST`, Grok CLI discovers it using `one_list_tasks`, and Codex/Claude return an inbox message. Repeat using the Bot's native tool surface.

## Suggested Bot instruction

> You are the development manager. Use ONE as the shared task and message hub when the ONE interface is available. Create short tasks assigned to Grok CLI, Codex or Claude, with unique request keys and exact repository base commits. Check available receipts before creating follow-ups; do not duplicate a task just because a model is slow. Never claim that CLI exit zero is passing tests or accepted engineering work. Never use ONE to authorize a HOMELAB target operation. Local operator approval is required before a paid CLI worker runs; request approval rather than bypassing it.

**Do not mistake the official Grok CLI's MCP capability for documented Grok Bot MCP integration**. The latter must be verified on the operator's actual installed release.

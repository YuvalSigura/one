# Windows Copilot ONE MCP startup — exact recovery steps

This is an operator runbook, not an automatic configuration migration.
The GitHub repository cannot modify files in the operator's Windows profile.
An already running MCP host cannot be reconfigured from the ONE child process.

## Root cause seen in the supplied Copilot process logs

Copilot discovers ONE, but ONE exits before initialize. The effective
ONE_DB was malformed and pointed at a directory, not a SQLite file:

    ' C:\Users\yuval\.one'

The intended authoritative SQLite FILE is normally:

    C:\Users\yuval\.one\state.db

Check this file actually exists before changing a live client. Do not silently
create another SQLite database: that would split ONE state across stores.
This branch trims outer whitespace in ONE_DB and explains directory misconfiguration.
A stale Copilot CLI session must STILL be restarted after its config is corrected.

## Operator-only local steps (PowerShell; NO provider launches)

1. Close the existing Copilot CLI session cleanly first; do not kill unrelated
   processes.
2. Confirm the expected database and executable exist:

       Test-Path -LiteralPath "$HOME\.one\state.db" -PathType Leaf
       Test-Path -LiteralPath "$HOME\Documents\GitHub\one\.venv\Scripts\python.exe" -PathType Leaf

   If the file is absent, STOP and locate the existing authoritative ONE
   database before creating or changing anything.

3. Back up the Copilot MCP config:

       Copy-Item -LiteralPath "$HOME\.copilot\mcp-config.json" -Destination "$HOME\.copilot\mcp-config.json.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"

4. Edit ONLY the existing one entry in the mcpServers object of
   %USERPROFILE%\.copilot\mcp-config.json. Preserve other MCP servers:

       "one": {
         "command": "C:\\Users\\yuval\\Documents\\GitHub\\one\\.venv\\Scripts\\python.exe",
         "args": ["-m", "one_mcp.server"],
         "env": {
           "ONE_DB": "C:\\Users\\yuval\\.one\\state.db"
         }
       }

   This is a snippet of the mcpServers object, NOT the whole file. JSON
   requires doubled backslashes in Windows paths. No leading/trailing spaces.

5. Back up %USERPROFILE%\.codex\config.toml and edit only the existing
   mcp_servers.one section. Do NOT append a duplicate TOML section:

       [mcp_servers.one]
       command = 'C:\Users\yuval\Documents\GitHub\one\.venv\Scripts\python.exe'
       args = ['-m', 'one_mcp.server']
       [mcp_servers.one.env]
       ONE_DB = 'C:\Users\yuval\.one\state.db'

   Keep any unrelated fields and the original valid TOML structure. The
   reported Codex command had an invalid leading space INSIDE its quotes.

6. Start a NEW Copilot session. Verify the ONE connection now survives
   initialize and offers ONE's tools/list. Check fresh process logs.
   An existing Copilot session can retain old effective settings.

7. Claude Code MCP registration is independently configured and optional.
   Neither Copilot nor Codex registration implicitly registers ONE for Claude.

## Explicit boundaries

- Do not launch paid providers; MCP initialization and tools/list are enough.
- Do not create a new empty SQLite database if the expected existing one is missing.
- Never rewrite the entire client configuration or drop unrelated server entries.
- Do not merge, push or modify the ONE main branch merely to do client repair.
- CI demonstrates ONE stdio initialization with outer whitespace surrounding
  a valid SQLite FILE path, and a clear error for a directory path. It does NOT
  prove the operator's running Copilot process has been restarted successfully.

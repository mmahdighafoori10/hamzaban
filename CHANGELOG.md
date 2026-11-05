# Changelog

## 1.0.0 — first public release

**Shared memory**
- One neutral pack (`pack.json`) rendered in the dialect of Claude Code, Claude.ai, Codex, ChatGPT, Gemini, Cursor, Copilot, three chat-API shapes and plain text; new agents are one JSON file.
- The closing block (`hamzaban-close`) brings web chats in; facts and decisions wait in `30-Pending` until approved.
- `native_memory.py` proposes the agents' own memories (Claude Code notes, Codex memories) to the shared notebook.
- A read-only MCP server (`infinity-memory`) for Claude Code, Codex and Claude Desktop.

**Chats**
- Stable tags for every chat (`CC-7K3QF`) and sub-tags for topics (`CC-7K3QF/T3`), read from Claude Code, Codex and Chat On Steroids logs.
- Cut-off detection: usage limits (with the time the tokens return), refused requests, stopped turns, unfinished tool calls and unanswered messages; transient errors that the agent recovers from are not reported.
- Resume text by tag or topic (`memory_handoff`, `hamzaban-continue`).

**Skills and control room**
- Skills kept in step between agents without ever deleting or overwriting; respects Chat On Steroids' limits.
- An offline control room (chats, "now", skills matrix, memory path).

**Installer**
- `install.py` / `INSTALL.cmd`: dry run, per-step consent, backups before every change outside the Vault, `--uninstall`, `--doctor`. Windows task, launchd and cron backends.

**Not in this release**
- No network sync, no web chats tracking, no English control-room text (the page is Persian, RTL). macOS and Linux are experimental.

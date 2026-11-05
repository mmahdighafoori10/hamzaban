# Changelog

## Unreleased

**Continuity packages**
- `continuity.py` / `hamzaban continuity write`: a portable `project-continuity/` folder for one project or one chat (README, INSTRUCTIONS, CONTEXT, a dated HANDOFF, DECISIONS, TEST-LOG, OPEN-QUESTIONS), filled from the Vault, the chat registry and read-only git and manifest facts, with five evidence labels. What needs a look at the code stays `TODO(agent)`.
- A cut-off chat's handoff says why it stopped and when the usage window opens again. Chats and proposals from other projects are counted, never quoted. A file you edited is never overwritten (the fresh copy lands beside it as `<name>.generated.md`).
- `continuity.py parity` / `hamzaban parity`: do `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` and the other harness files carry the user's rules? States `CURRENT`, `STALE`, `OWN`, `MISSING`, `MIXED`; `--home` checks the three agents' own folders; `--strict` for CI.
- New skill `hamzaban-continuity`: how an agent completes and uses a package safely. Installed by the installer with the others.

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

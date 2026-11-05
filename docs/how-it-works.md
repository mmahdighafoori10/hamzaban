# How it works

Hamzaban is a set of small Python scripts (the *engine*) that live in `<Vault>/.ai-memory` and a folder of Markdown files (the *Vault*) that
they read and write. A background task runs one pass of `autosync.py` every minute; the agents reach the memory through hooks, an MCP server and
plain files. Nothing talks to a network.

```text
                     ┌────────────────────────── your computer ───────────────────────────┐
 Claude Code ──hook──►  capture_session ─┐                                                 │
 Codex       ──logs──►  chat_sources ────┤                                                 │
 Chat On Steroids ────►  auto_capture ───┤──►  Vault (Markdown)  ◄── you edit / approve    │
 agents' own memories ─► native_memory ──┘      00-Context/Harness.md   your standing rules │
                                                10-Facts/        approved facts            │
                                                20-Checkpoints/  state of each project     │
                                                30-Pending/      proposals waiting for you │
                                                40-Conversation-Capture/  your messages    │
                                                50-Handoff/      one pack, one file per agent
                                                60-Chats/        one note per chat (tag)   │
                              ┌─────────────────────────────┘                              │
                              ▼                                                            │
   infinity_mcp.py (MCP, read-only)  ──►  Claude Code · Codex · Claude Desktop · Cursor    │
   claude_hook.py (session start)     ──►  Claude Code                                     │
   50-Handoff/latest/<agent>.md       ──►  paste / AGENTS.md / GEMINI.md / .cursor/rules   │
   control-room.html                  ──►  you                                             │
                     └─────────────────────────────────────────────────────────────────────┘
```

## The pieces

| Piece | What it does |
|---|---|
| **Vault** | Plain Markdown you own. Created by the installer; Obsidian-compatible, Obsidian not required. |
| **Capture** (`capture_session.py`, `auto_capture.py`, `claude_hook.py`) | Saves your messages and checkpoints of Claude Code and Chat On Steroids chats as Markdown, redacted. |
| **Chat registry** (`chats.py`, `chat_sources.py`, `chat_topics.py`) | Reads the agents' own logs read-only, gives each chat a tag and each topic a sub-tag, decides whether a chat is active, cut off, idle or closed, and writes `60-Chats/<tag>.md`. |
| **Index** (`infinite_context.py`) | A SQLite full-text index of the Vault, used by `search` and `resume`. |
| **Pending → facts** (`pending.py`) | Every proposal waits in `30-Pending`. `approve` moves it to `10-Facts`; `reject` moves it to `30-Pending/rejected`. Nothing is deleted. |
| **Hamzaban** (`hamzaban/`) | Builds one neutral *pack* from the Vault and renders it in each agent's dialect; parses the closing block. Format: [`engine/hamzaban/SPEC.md`](../engine/hamzaban/SPEC.md). |
| **MCP server** (`infinity_mcp.py`) | Read-only tools for agents: `memory_search`, `memory_read`, `memory_context`, `memory_chats`, `memory_handoff`, and `memory_propose` (which only stages a proposal). |
| **Native memory import** (`native_memory.py`) | Turns the notes Claude Code keeps for itself and Codex's extracted memories into proposals. |
| **Skill sync** (`autosync.py`, `suite.py`) | Keeps skills in step between agents' skill folders and `skills-src`. Never deletes; a skill that differs in two places becomes a note, not an overwrite. |
| **Control room** (`control_room.py`) | Generates `00-Dashboard/control-room.html`: chats and tags, skills matrix, memory path, and for every chat a *where is it?* panel. |
| **Application** (`app_window.py`, `app_links.py`, `shortcuts.py`) | Windows: the icon, the Start-menu entry and the desktop shortcut, the control room as its own window, and the `hamzaban:` link behind the *where is it?* buttons. See [app.md](app.md). |
| **Doctor** (`suite.py doctor`) | Nine checks, always exit code 0: capture, hooks, registrations, skills, index, queue, autosync. |

## How a chat is classified

The registry never asks an agent anything; it reads what the agent wrote down.

- **active**: the chat's log was written within the last 15 minutes. Several chats can be active at once.
- **interrupted** (cut off), with the reason the log gives: *limit* (Codex `task_complete.error = usage_limit_exceeded`, Claude Code's "hit your limit" row, a
  response cut by `max_tokens`), *error* (a refused or lost request, after a 2-minute grace period for retries), *aborted* (you pressed stop), *tool* (a tool
  call never returned), *turn* (a turn started and never finished), *unanswered* (your last message got no reply).
  An error followed by the agent carrying on is a hiccup and clears itself.
- **idle**: nothing unusual; **closed**: it ended with a `hamzaban-close` block; **continued**: a newer chat started with "continue from this tag".

A usage limit is shown the moment it happens, together with the time the tokens come back when the log says so.

## What it never does

- never deletes your notes, skills or agent files; changes outside the Vault are backed up first;
- never opens a network connection; the control room page loads nothing remote;
- never writes to an agent's own session or memory folders (those are read-only; the SQLite files are read from a temporary copy);
- never turns anything into a *fact* without your approval.

## Where each agent gets its memory

See [agents.md](agents.md).

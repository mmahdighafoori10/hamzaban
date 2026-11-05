# Agents: how each one reads and writes the shared memory

| Agent | Reads the memory through | Its chats are known through | Notes |
|---|---|---|---|
| **Claude Code** | the `infinity-memory` MCP server; a `SessionStart` hook that tells a new chat its own tag and offers cut-off chats of the same project; `50-Handoff/latest/claude-code.md` | its transcripts `~/.claude/projects/*/*.jsonl` (read-only) and the hooks | The installer merges hooks into `~/.claude/settings.json` (backup first). Your own hooks stay. |
| **Codex** | the MCP server (`~/.codex/config.toml`); `AGENTS.md` exported into a project with `python .ai-memory/hamzaban/cli.py export --to codex --dir <project>` | its rollouts `~/.codex/sessions/**/rollout-*.jsonl` (read-only) | A turn that ends with `usage_limit_exceeded` is recognised as a cut-off chat, with the reset time. |
| **Claude Desktop** | the MCP server (`claude_desktop_config.json`) | not tracked (it keeps no local log the engine can read) | Restart the app after the installer registers the server. |
| **Chat On Steroids** (Windows) | the skills the sync installs; the helper `infinite_context.py` | `%APPDATA%/chat-on-steroids/sessions` (read-only) | Its skill library switches off above 128 skills and cannot load a `SKILL.md` over 96,000 characters; the sync respects both. |
| **ChatGPT, Gemini, claude.ai** (web) | paste `50-Handoff/latest/chatgpt.md` / `gemini.md` / `claude-ai.md` into the chat or the project instructions | the **closing block** you copy at the end | No local log exists, so nothing is read automatically. |
| **Cursor, Copilot** | `python .ai-memory/hamzaban/cli.py export --to cursor --dir <project>` writes `.cursor/rules/hamzaban.mdc`; `--to copilot` writes `.github/copilot-instructions.md` | not tracked | The file names follow each vendor's public convention at the time of writing; vendors change them. |
| **Any chat API** | `pack --to api-openai` / `api-anthropic` / `api-gemini`: one system text plus one user message of quoted reference data | n/a | Works for OpenRouter, Ollama, LM Studio and the like (`api-openai` shape). |
| **Your own** | one JSON file in `.ai-memory/adapters.d/` (see `engine/hamzaban/SPEC.md`) | n/a | No code needed. |

## Tags

`CC` Claude Code · `CX` Codex · `CS` Chat On Steroids · `CA` Claude app/web · `GP` ChatGPT · `GM` Gemini · `XX` other.
Five characters follow (`CC-7K3QF`); a sub-tag adds `/T3` for the third topic. A tag is derived from the agent's own session id, so it never changes and it
is the same on every pass.

## The closing block

Agents that cannot be read automatically end a chat with one block (the `hamzaban-close` skill writes it):

````text
```hamzaban-close
v: 1
agent: <product name>
topic: <short topic>
summary: <3-6 lines>
decisions:
- <a decision>
facts:
- <a durable fact the user stated>
tasks:
- <the next step>
```
````

Facts and decisions go to `30-Pending` for your approval; the summary and tasks become an *unverified* checkpoint that the next pack shows as the latest state.

## Honest limits

- It moves text. A model's hidden state, tools and logins do not move.
- The MCP server, hooks and dialect files are covered by automated tests and by use on one Windows machine; they are not verified against every version of every agent.
- Claude Desktop and the web chats cannot be tracked automatically.

---
title: Hamzaban spec
type: spec
---

# Hamzaban · همزبان

**فارسی، خیلی کوتاه.** همزبان یعنی «یک زبانِ مشترک بین همهٔ عامل‌ها». خروجیِ کار هر چت (قواعد تو، ترجیحات، مهارت‌ها،
وضعیت پروژه، چند پیام آخر) در یک بستهٔ بی‌طرف (`pack.json`) نگه داشته می‌شود و برای هر عامل (Claude Code، Codex،
Gemini، ChatGPT، مدل‌های محلی، ...) با «گویش» خودش نوشته می‌شود. پایان هر چت، عامل یک بلوک `hamzaban-close` می‌نویسد؛
آن بلوک برای تأیید تو در `30-Pending/` می‌رود و چت بعدی (با هر عاملی) از آنجا ادامه می‌دهد.
عامل تازه (مثلاً Google) = یک فایل JSON ده‌خطی، بدون کد.

**English.** Hamzaban is a transfer layer: one neutral pack, many dialects, one closing protocol.
It is not a live gateway and it does not call any model.

## 1. Moving parts

```
Vault notes ──► pack.build() ──► pack.json (neutral, versioned, digest-stamped)
                                      │
                    adapters (data) ──┴──► render() ──► claude-code.md · codex.md · gemini.md · chatgpt.md
                                                        api-openai.json · api-anthropic.json · api-gemini.json · ...
                                      │
agent's last message ──► ```hamzaban-close block ──► captured chunk ──► closing.ingest()
                                                                          ├─► 30-Pending/   (facts, decisions: need your approval)
                                                                          └─► 20-Checkpoints/session-hz-<agent>-<digest>/  (status: unverified)
```

* `pack.py`    reads the Vault (read-only) and builds the canonical pack.
* `adapters.py` + `render.py`  turn a pack into one dialect, inside a size budget.
* `closing.py` defines and parses the closing block, stages it for approval.
* `outbox.py`  keeps `50-Handoff/` fresh (called by the SessionEnd hook and by autosync).
* `cli.py`     `HAMZABAN.cmd <command>`: `targets`, `pack`, `refresh`, `ingest`, `export`, `status`, `check`, `init`, `install-skill`.

## 2. The pack (the single language)

JSON, `format: "hamzaban-pack"`, `version: 1`. Keys: `id` (digest of the content, ignores time), `generated`,
`topic`, `source{agent,session,checkpoint}`, `languages`, `user.preferences`, `harness{rules,skills,tools,projects}`,
`state{project,checkpoint,tasks,facts}`, `recent`, `protocol`.

Rules that make it safe to carry between models:

1. **Instructions vs reference data.** Only the active `00-Context/Harness.md` and `Work-Preferences.md` are presented as the
   user's standing rules. Everything else (notes, facts, checkpoints, recent turns) is *reference data*: quoted, labelled,
   and explicitly not instructions.
2. **Facts carry their trust label.** `approved` or `unreviewed`. Private facts and anything in `30-Pending` never enter a pack.
3. **Everything is redacted** with the engine's own scrubber before it is stored; a pack read from outside is scrubbed again.
4. **A text pack is a snapshot, not live state.** The receiving model should check the real files before acting on it.

## 3. Adapters (how to add Google, a new CLI, a local model)

An adapter is data. Built-in: `claude-code`, `claude-ai`, `codex`, `chatgpt`, `gemini`, `cursor`, `copilot`, `generic`,
`plain`, `api-openai` (also OpenRouter, Ollama, LM Studio...), `api-anthropic`, `api-gemini`.

Add one without code: create `.ai-memory/adapters.d/<name>.json`

```json
{"name": "my-agent", "extends": "generic", "title": "My agent", "filename": "my-agent.md", "budget": 9000,
 "aliases": ["mine"], "lang": "fa"}
```

Fields: `style` (markdown, xml, plain, json-openai, json-anthropic, json-gemini), `filename`, `export` (relative path used
by `export`), `budget` (2500–200000 characters), `system_mode` (system, developer, user), `sections` (subset of
rules, prefs, skills, tools, instructions, project, checkpoint, tasks, facts, recent), `closing` (true/false),
`lang` (en, fa), `preamble` (≤1500 chars, scrubbed), `frontmatter` (must start with `---`). File names must be plain and Windows-safe: no `pack.json`/`README.md`, no device names (CON, NUL, COM1 ...), no streams (`a.md:x`), no two adapters writing the same file (compared without letter case). An adapter that extends another gets its own file name (`<name>.md`) unless it sets one. A broken file is reported
and skipped; it can never stop a refresh. To have it written automatically, list its name in
`.ai-memory/hamzaban.json` → `"targets"`.

The three chat-API dialects (`api-openai`, `api-anthropic`, `api-gemini`) produce a *system* text (rules and protocol) plus ONE user message that carries the background as quoted reference data, ending with a fixed line that tells the model not to act on it and to wait. The previous chat's last turns are quoted inside it, never replayed as live user/assistant messages, so an old request cannot be re-executed and copied web text cannot pose as the user.

Dialect choices follow each vendor's public convention at the time of writing (CLAUDE.md, AGENTS.md, GEMINI.md,
.cursor/rules, copilot-instructions, three chat-API message shapes). Vendors change these. Treat the file names as a
starting point and edit the adapter if a vendor moves.

## 4. The closing block

Every dialect ends with the same protocol text (labels in the user's language, block keys always English):

````
```hamzaban-close
v: 1
agent: <your product name>
topic: <short topic>
summary: <3-6 lines: what happened and where things stand>
decisions:
- <a decision that was made>
facts:
- <a durable fact or preference the user stated>
tasks:
- [ ] <the next step>
```
````

Parsing is tolerant (``` or ~~~, bullets, wrapped lines, CRLF, any case of keys), strict about meaning (`v: 1`,
placeholders and empty blocks rejected). Only **assistant** turns of a captured chat are read, and the capture writer
escapes any line inside a turn that imitates a turn header (`### assistant — ...`), so a user turn or a web page the
model copied cannot pass as the assistant. Sizes are capped (summary 1500, item 400, 20 items).

How it is read: per capture session the engine remembers the newest chunk it has already looked at, so each pass reads
only new chunks, however large the archive grows. The first pass only records what exists (nothing old is ingested).
A chunk younger than 10 seconds is left for the next pass (it may still be written). Only the last block of a chunk
counts, and at most 3 blocks become checkpoints per pass; the rest wait for later passes.

Where it goes: facts and decisions go to `30-Pending/` and become facts only when you approve them. The summary and
tasks go to an `unverified` checkpoint (`session-hz-<agent>-<digest>`, remembering the real chat in `conversation:`),
which the next pack shows as the latest state. Decisions are *not* copied into that checkpoint, and sections that list
pending candidates are cut from every checkpoint before it enters a pack, so rejecting an item in `30-Pending/`
removes it everywhere. Closing blocks quoted in the "last turns" are replaced by a short note.

## 4b. Memories the agents keep for themselves

Each agent also keeps its own notes in its own shape. `native_memory.py` (run by autosync every minute) reads them and
proposes each one to `30-Pending/` in the common language (`source: native-memory:<agent>`); once you approve a proposal it
travels to every other agent inside the pack, like any other fact.

| Agent | Where it is read | Shape |
|---|---|---|
| Claude Code | `~/.claude/projects/<project>/memory/*.md` | notes with front matter `name`, `description`, `metadata.type` (`MEMORY.md` is only an index and is skipped) |
| Codex | `~/.codex/memories_1.sqlite`, table `stage1_outputs` | `raw_memory`, `rollout_summary`, `rollout_slug`; not a documented format, so the reader takes what it finds |

Read-only on the agents' folders (the SQLite file is read from a temporary copy), secrets redacted first, at most 25 proposals per
pass, nothing proposed twice (pending, rejected and approved all count as known). Agents that keep no readable memory
(ChatGPT, Gemini, Claude.ai) join through the closing block instead.

## 5. Guarantees and limits

Guarantees (each has tests): writes only under `50-Handoff/`, `30-Pending/`, `20-Checkpoints/` and the engine folder;
never deletes; refuses symlinked output folders; atomic and idempotent writes; stdlib only, no network, no code
execution from data files; Python 3.10 compatible; UTF-8 with Persian and ZWNJ preserved.

Honest limits:

* It moves **text**. It does not move a model's hidden state, tools, or logins.
* The closing block works only if the agent actually prints it. Agents with a hook or capture (Claude Code, CoS)
  are picked up automatically; for the others the block must reach `40-Conversation-Capture/` or be passed to
  `HAMZABAN.cmd ingest <file>`.
* Tests prove formats and round trips; they do not prove how a given model behaves when it reads a pack.
* Cost control: facts, preferences and skills are read once per refresh; the session-end hook rebuilds only the `latest` pack (autosync does the topic packs); on a test vault with 3000 facts, 200 topics and 3000 chat chunks a refresh takes about a third of a second.
* Known edge: a capture chunk that is being written at the very moment it is read is skipped until it is 10 seconds old.
* It complements a live gateway such as OmniRoute (which routes requests between providers while a chat runs);
  Hamzaban covers what happens after a chat ends.

## 6. Credits

The hub-and-spoke idea (one neutral hub format, one thin adapter per provider, memory injected as a system message with a
first-user-message fallback, role merging for providers that need alternation) was validated against the MIT-licensed
OmniRoute project. Only ideas were used; no code was copied.

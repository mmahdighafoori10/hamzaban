---
name: infinite-context
description: Retrieve earlier project context from the user's local Hamzaban Vault when they continue earlier work, mention old decisions, or the conversation was compacted; and record a verified checkpoint at milestones. Invoke even when the user does not name this skill.
---

# Infinite context — durable continuity from the user's Vault

The name describes an *experience*, not an unbounded context window: keep the live prompt small by retrieving the
relevant evidence from storage when it is needed.

## Where the memory is

The Vault (plain Markdown, only on this computer): `{{VAULT}}`
The engine: `{{ENGINE}}`

If your product has the MCP server `infinity-memory`, use its tools (they are read-only except `memory_propose`):
`memory_search` (find notes), `memory_read` (open one), `memory_context` (a budgeted brief), `memory_chats` (the chats this
computer knows, with tags), `memory_handoff` (a resume text for a chat tag or a topic), `memory_propose` (stage a fact for
the user's approval; it is never a fact by itself).

Without MCP, run the helper (Python 3.10+):

```
python "{{ENGINE}}/infinite_context.py" resume "<project name>" --budget 6500
python "{{ENGINE}}/infinite_context.py" search "<what you are looking for>"
```

## Retrieve at the beginning

When the user returns to an ongoing multi-session project or refers to an earlier decision, retrieve the relevant notes
**before** you change anything or state a historical fact. Skip it for a new one-off request.

Read the most relevant original notes. Notes with `source: reconstructed-conversation-summary` or an `unverified` checkpoint
are secondary evidence: separate verified decisions, hypotheses and open items, cite the note path, and prefer the current
files and the user's newest explicit instruction when they disagree.

## Preserve at milestones

At a substantial handoff, a long coding session, a "save context" request, or just before a topic switch, write a short,
**verified** snapshot (what is done, what remains, blockers, evidence paths):

```
python "{{ENGINE}}/infinite_context.py" checkpoint --project "<project>" --summary "What was actually completed, in which files" --decision "A choice the user confirmed" --blocker "A known problem" --next "The next concrete step"
```

Checkpoints are append-only; an older one is never overwritten. Never store passwords, keys or other secrets: the helper
redacts common patterns but cannot catch everything.

## Limits

This skill has no host hooks: it cannot push text into a live context or force a compaction, and a capture is the
user's own messages and checkpoints, not a full transcript. When resuming old work, retrieve explicitly and check the
real state of the files.

To wrap a chat up for the shared memory ("wrap up for memory", "hamzaban close"), follow the `hamzaban-close` skill.
To continue a chat by tag ("continue from CC-7K3QF"), follow the `hamzaban-continue` skill.

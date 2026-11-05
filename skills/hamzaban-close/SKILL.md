---
name: hamzaban-close
description: Wrap up this chat for the user's shared memory (Hamzaban). Use when the user says "wrap up for memory", "save this chat to memory", "hamzaban close", "پایان چت" or "جمع‌بندی برای حافظه". Writes ONE closing block the user copies into the Vault; nothing becomes a fact until the user approves it.
---

# hamzaban-close — wrap up a chat for the shared memory

The user calls this at the end of a chat. Your whole job is to write **one** fenced block. You do not write files,
call tools, or save anything yourself: the user copies the block and approves it later.

## What to do

1. Look only at what actually happened in THIS chat. Do not invent or guess.
2. Answer with exactly one fenced block in this shape (the keys stay in English; write the content in the language
   of the user's own notes, short and plain):

```hamzaban-close
v: 1
agent: <your product name, e.g. chat-on-steroids>
topic: <short topic, at most 80 characters>
summary: <3-6 lines: what happened and where things stand>
decisions:
- <a decision that was made>
facts:
- <a durable fact or preference the user stated>
tasks:
- <the next step>
```

   Replace every `<...>` with real content. Drop a list's bullets if there is nothing honest to put in it.
3. Rules for the content:
   - `facts`: only what the USER said or confirmed in this chat. Your own conclusions go in `summary`, not in `facts`.
   - Never include passwords, API keys, tokens, wallet or seed phrases, or the content of secret files (such as `.env` files).
   - No private data about other people.
4. After the block, add one short sentence: copy the block above, then on Windows double-click `HAMZABAN-CLOSE.cmd` in the
   `.ai-memory` folder (it reads the clipboard); anywhere else save it to a text file and run
   `python .ai-memory/hamzaban/cli.py ingest <file>`. It then waits in `30-Pending` for the user's approval.
   (If the user writes in Persian, say it in Persian: «بلوک بالا را کپی کن و فایل `HAMZABAN-CLOSE.cmd` را در پوشهٔ `.ai-memory` دوبار کلیک کن.»)

Do not add anything else: no second block, no long explanation.

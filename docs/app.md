# The application: icon, shortcuts and "where is this chat?"

On Windows Hamzaban installs like an application: its own icon, an entry in the Start menu, a shortcut on the desktop and its own window. In that window every chat has a
**کجاست؟** ("where is it?") button. It tells you which application the chat took place in and which part of the chat to look at, and takes you there as far as that
application allows.

macOS and Linux get no shortcuts and no `hamzaban:` link yet. The control room is still the HTML file in your Vault, and its panel still names the application and the part of the chat and has the copy buttons.

## What the installer adds (Windows)

The `shortcuts` step of `python install.py` asks first (`--skip shortcuts` leaves it out). For the current Windows user only, and without administrator rights, it creates:

| What | Where | What it is for |
|---|---|---|
| a Start-menu entry named **Hamzaban همزبان** | the *Programs* folder of your Start menu | typing either spelling into the Start menu finds it |
| a desktop shortcut with the same name and the same icon | your Desktop (the real one, also when it has moved into OneDrive) | one click |
| the `hamzaban:` link | `HKEY_CURRENT_USER\Software\Classes\hamzaban` | lets the buttons of the control room open an application or show a file ([below](#the-hamzaban-link)) |

Both shortcuts start `hamzaban_app.pyw` through `pythonw`, so no console window flashes. It rebuilds the control room page when that is older than ten minutes and opens it in Microsoft
Edge (or Chrome) as an application window: no tabs, no address bar, and a browser profile of its own (`%LOCALAPPDATA%\Hamzaban\app-profile`), so closing it never touches your everyday
browser. With neither browser installed the page opens in your default browser instead; `HAMZABAN_BROWSER` can name a browser executable explicitly. The page reloads itself every minute,
except while a panel is open.

Without the installer, from the Vault's `.ai-memory` folder:

```text
python shortcuts.py install      --no-desktop, --no-menu or --no-links leave one out; --dry-run shows what would happen
python shortcuts.py status       what is in place
python shortcuts.py remove       take it all away again
```

`python install.py --uninstall` removes the shortcuts and the link too. It leaves the window's profile folder alone (it holds nothing but that window's browser cache); delete it by hand if you want it gone.

## "Where is this chat?"

Every chat card, every part (topic) of a chat and every entry of the *now* list has a **کجاست؟** button. It opens a panel with:

- **the application** the chat was written in, and the entry point the agent's own log names: the Claude desktop app (Code tab), Claude Code in a terminal or an editor, the Codex app, Codex in a terminal,
  Chat On Steroids; for chats that came in through a closing block, Claude, ChatGPT or Gemini;
- the chat's **title**, the **time** it ran, its **working folder**, the **chat file** (the agent's own log) and the chat's **id** in that application, each with a copy button;
- **which part of the chat**: every part lists the numbers of your messages it covers ("messages 14 to 22 of your 40"), when it ran, your first request and the last result in it, and a **search phrase**, the first
  words of that request, with a copy button. In the application, press Ctrl+F and paste it;
- the buttons that make sense for that chat:

| Button | What it does | Offered when |
|---|---|---|
| open this very chat | opens the chat in the Codex app through `codex://threads/<id>`, the link Codex documents | a Codex-app chat, and the `codex:` link is registered on this computer |
| open the application | starts the application from the Start menu; for a web chat, opens the website | the chat has an application or a website, and no link to the chat itself |
| show in Explorer | opens Explorer with the chat's log selected (a Codex log that has been packed into `.jsonl.zst` is found too; for Chat On Steroids it is the session folder) | the log is still there |
| open the folder | opens the chat's working folder | the chat has one, and it still exists |
| open in a terminal | opens Windows Terminal (or `cmd`) in the working folder and runs `claude --resume <id>` or `codex resume <id>` | a Claude Code or Codex chat whose command is installed; otherwise the command is shown for you to copy |

Honest limits:

- Only Codex has a link to one chat. The Claude desktop app has none for an old Code session (only one that starts a new chat) and neither has Chat On Steroids: the application opens and you find the
  chat by its title or by the search phrase. No application jumps to an exact message; the message numbers and the search phrase are signposts.
- The Codex link is the one Codex documents. How far a given version of the app follows it is up to the app.
- Web chats (claude.ai, ChatGPT, Gemini) are known only from their closing block. They have no log, so the panel can name the service and open the app or the website, nothing more.
- Everything in the panel comes from the chat registry, that is from the agents' own logs, and the file and the folder are looked for on the disk every time the page is built. What is gone gets no button
  but a note, *دیگر در این مسیر نیست* ("no longer at this path"), and its path stays visible for copying. The scratch folders of Claude desktop sessions, for one, are often gone by the time you look.

## The `hamzaban:` link

The buttons that need your computer are links such as `hamzaban://reveal?tag=CC-7K3QF`. Windows hands them to `hamzaban_open.pyw`, which does one of four things:

| Link | Does |
|---|---|
| `hamzaban://app?tag=<tag>` | opens the application the chat belongs to (or its website) |
| `hamzaban://reveal?tag=<tag>` | shows the chat's log file in Explorer |
| `hamzaban://folder?tag=<tag>` | opens the chat's working folder |
| `hamzaban://resume?tag=<tag>` | opens a terminal in the working folder and runs the resume command |

(`app` also accepts `?agent=<name>` instead of a tag.) Anything else, an unknown action, an extra parameter, a malformed tag, is refused with a message box. There is no server and no open port: Windows
starts a short-lived process for each click, and it makes no network connection.

Why it is safe to have such a link on your computer:

- it carries **an action and a chat tag, nothing else**: never a path, a command or a web address;
- the tag is looked up in your own chat registry, and the folder, the file, the application and the command all come from there. A tag that is not in your memory does nothing;
- the resume command is built from a fixed pattern (`claude --resume <id>` or `codex resume <id>`) and the id has to pass a strict check;
- so a web page that fires such a link at your browser can at most open an application or a folder you already have, or continue one of your own chats, and only when it already knows a valid tag;
- it is registered for your Windows user only.

## Limits

- Windows only for now.
- One Windows user at a time: each account installs its own shortcuts and link and uses its own Vault. The background sync task has a single fixed name (`MemorySuiteAutoSync`), so two accounts on
  one computer cannot both schedule it yet.
- The list of applications comes from the Start menu (`Get-StartApps`) and is remembered for twelve hours in `.ai-memory/app-launchers.json`. An application that is not in the Start menu is not found;
  the button then opens its website when it has one, and otherwise says so.
- The window needs Edge or Chrome (Windows 10 and 11 include Edge); without them the page opens in your default browser, with tabs and an address bar.

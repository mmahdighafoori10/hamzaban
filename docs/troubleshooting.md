# Troubleshooting

Start with the health check; it never changes anything and always exits 0:

```text
python install.py --doctor
```

`PASS` is good, `UNKNOWN` means "cannot be seen from here, or nothing has happened yet", `WARN` and `FAIL` come with the reason.

| Symptom | What to do |
|---|---|
| `Python was not found` / the installer window closes at once | Install Python 3.10+ from python.org and tick *Add python.exe to PATH*. The Microsoft Store "python" stub does not count: install the real one. |
| Windows says the file is from the internet / SmartScreen warns | Right-click the ZIP → *Properties* → *Unblock* before unzipping, or run `python install.py` from a terminal. Nothing in the project is compiled or signed. |
| The agent does not see the memory server | Fully quit Claude Desktop (tray icon → Quit) and open it again; start a **new** Codex / Claude Code chat. Check `python .ai-memory/connect_all.py --dry-run` says `already present`. |
| Hooks seem silent (`3. Hook activity` is UNKNOWN) | Nothing has run since you registered them. Open a new Claude Code chat; `.ai-memory/logs/hook-events.jsonl` should appear. |
| `5. Codex hook reference` is WARN | Codex has neither the memory server nor a hook. Run the installer again with the `mcp` step, or add `[mcp_servers.infinity-memory]` yourself (see `connect_all.py`). |
| `6. Skills drift` is WARN | Wait two minutes: the background sync leaves files younger than 20 seconds alone. If it stays, `python .ai-memory/suite.py skills diff` shows what differs; `skills push` (dry-run by default) fixes it. |
| `9. Autosync` is WARN or FAIL | The background task is not running. Windows: `powershell -ExecutionPolicy Bypass -File .ai-memory\manage_autosync.ps1 -Action status`. macOS: `launchctl list | grep hamzaban`. Linux: `crontab -l`. Or run `python .ai-memory/autosync.py once` yourself. |
| A chat is missing from the control room | It is older than 120 days, or its agent keeps no readable log (web chats, Claude Desktop). Run `python .ai-memory/chats.py scan` and `python .ai-memory/chats.py list --all`. |
| A chat shows the wrong state | `python .ai-memory/chats.py show <TAG>` lists its topics; `resume <TAG>` prints what the registry knows. The state comes only from the agent's own log. |
| The control room shows strange spacing around English words | It is right-to-left Persian text; the page isolates Latin words, but a very old browser may not. Use a current Edge, Chrome or Firefox. |
| *Hamzaban* is not in the Start menu, or there is no desktop shortcut | Windows only. `python .ai-memory/shortcuts.py status` says what is missing and `python .ai-memory/shortcuts.py install` adds it (or run the installer again and accept the `shortcuts` step). |
| A button of the *کجاست؟* panel does nothing, or the browser asks which app to use | The `hamzaban:` link is not registered for this Windows user: `python .ai-memory/shortcuts.py status`, then `install`. Your browser may ask once before it hands the link to Windows; allow it. |
| *Open this chat* opens the application but not the chat | Only Codex has a link to one chat. For the Claude desktop app and Chat On Steroids use the title and the search phrase (Ctrl+F) that the panel shows. See [app.md](app.md). |
| The application window does not open, or opens as a normal browser tab | It needs Microsoft Edge or Chrome (Windows includes Edge). Set `HAMZABAN_BROWSER` to the browser's `.exe` to pick one; without either, the page opens in your default browser. |
| `database is locked` in `logs/errors.log` | Two passes overlapped (rare). It retries on the next pass. If it persists, close other programs that open `.ai-memory/*.sqlite3`. |
| I want to start over | `python install.py --uninstall`, delete `<Vault>/.ai-memory`, run the installer again. Your notes in the other Vault folders are never touched by either step. |

## Logs

- `<Vault>/.ai-memory/logs/install.log` · what the installer did
- `<Vault>/.ai-memory/logs/errors.log` · errors from hooks and background passes
- `<Vault>/.ai-memory/logs/hook-events.jsonl`, `mcp-calls.jsonl` · what fired (no message content)
- `<Vault>/.ai-memory/autosync-status.json` · the last background pass

When you report a bug, attach `python install.py --doctor` and the last lines of `errors.log`, **after reading them**: they can contain file paths.

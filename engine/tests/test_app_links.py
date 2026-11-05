"""app_links.py and the control room's "where is it?" panel: which application a chat lives in, the links that take you there, and the
hamzaban: link handler (strict, and safe against hostile links). Temp dirs, fake registries and fake processes only: nothing is opened."""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import app_links as al  # noqa: E402
import chats  # noqa: E402
import control_room  # noqa: E402

THREAD = "0190d2c4-7b1e-7c3a-9d55-1f2e3a4b5c6d"           # made up, like every id and tag in the fixtures
SEGMENT = "0190d2c9-0a11-7e42-b6c7-8d9e0f1a2b3c"
CLAUDE_ID = "b7d1c0de-2f44-4a8b-9c13-5e6f7a8b9c0d"


def chat(**kw) -> dict:
    base = {"tag": "CX-9ZZ99", "agent": "codex", "agent_fa": "Codex", "entry": "vscode", "source_id": f"{THREAD}_{SEGMENT}", "cwd": r"C:\work\demo",
            "path": r"C:\Users\u\.codex\sessions\r.jsonl", "title": "A demo chat", "project": "demo", "turns": 71, "first_ts": "2026-10-27T09:49:00Z",
            "last_ts": "2026-11-05T09:26:00Z", "topics": []}
    return dict(base, **kw)


class FakeRegistry:
    def __init__(self, rows):
        self.rows = {r["tag"]: r for r in rows}

    def get(self, ref):
        return self.rows.get(str(ref).upper())


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kw):
        self.calls.append((args, kw))


class KeysAndCommands(unittest.TestCase):
    def test_the_application_comes_from_the_agent_and_its_entry(self):
        for agent, entry, want in (("claude-code", "claude-desktop", "claude"), ("claude-code", "cli", "claude-cli"), ("claude-code", "", "claude-cli"),
                                   ("codex", "vscode", "codex"), ("codex", "cli", "codex-cli"), ("codex", "exec", "codex-cli"), ("cos", "x", "cos"),
                                   ("claude-ai", "hamzaban-close", "claude-ai"), ("chatgpt", "", "chatgpt"), ("gemini", "", "gemini"), ("something", "", "")):
            with self.subTest(agent=agent, entry=entry):
                self.assertEqual(al.app_key(agent, entry), want)

    def test_a_codex_log_is_named_thread_underscore_part_and_codex_knows_the_thread(self):
        self.assertEqual(al.thread_id("codex", f"{THREAD}_{SEGMENT}"), THREAD)
        self.assertEqual(al.thread_id("codex", THREAD), THREAD)
        self.assertEqual(al.thread_id("claude-code", "abc_def"), "abc_def")

    def test_resume_commands_and_hostile_ids(self):
        self.assertEqual(al.resume_cmd("claude-code", CLAUDE_ID), f"claude --resume {CLAUDE_ID}")
        self.assertEqual(al.resume_cmd("codex", f"{THREAD}_{SEGMENT}"), f"codex resume {THREAD}")
        self.assertIsNone(al.resume_cmd("cos", CLAUDE_ID))
        for bad in ("x; calc", "a b", "../x", "", "ab", "id&&whoami", "id\nrm", 'id"x'):
            with self.subTest(bad=bad):
                self.assertIsNone(al.resume_cmd("claude-code", bad))
                self.assertIsNone(al.resume_cmd("codex", bad))

    def test_the_search_phrase_is_the_first_words_without_markdown_or_stray_marks(self):
        self.assertEqual(al.search_phrase("## **گزارش** جامع \\ 1 انتقال پروژه"), "گزارش جامع انتقال پروژه")
        self.assertEqual(al.search_phrase("one two three four five six seven eight nine"), "one two three four five six seven")
        self.assertLessEqual(len(al.search_phrase("x" * 300)), 60)
        self.assertEqual(al.search_phrase(""), "")
        self.assertEqual(al.search_phrase(None), "")


class Links(unittest.TestCase):
    def test_build_and_parse_round_trip(self):
        for action in al.ACTIONS:
            self.assertEqual(al.parse_uri(al.build_uri(action, tag="CC-4TQ8K")), (action, {"tag": "CC-4TQ8K"}))
        self.assertEqual(al.parse_uri(al.build_uri("app", agent="codex")), ("app", {"agent": "codex"}))
        self.assertEqual(al.parse_uri('"hamzaban://reveal?tag=cc-4tq8k"'), ("reveal", {"tag": "CC-4TQ8K"}), "Windows quotes the argument; the tag is upper-cased")
        self.assertEqual(al.parse_uri("HAMZABAN:folder/?tag=CC-4TQ8K")[0], "folder")

    def test_only_an_action_and_a_tag_or_an_agent_name_are_accepted(self):
        for bad in ("http://example.com", "hamzaban://", "hamzaban://reveal", "hamzaban://reveal?", "hamzaban://format?tag=CC-4TQ8K",
                    "hamzaban://reveal?path=C:/Windows/System32", "hamzaban://reveal?tag=CC-4TQ8K&path=x", "hamzaban://reveal?tag=CC-4TQ8K&tag=CC-4TQ8K",
                    "hamzaban://reveal?tag=", "hamzaban://reveal?tag=C-1", "hamzaban://reveal?tag=CC-4TQ8K%0Acalc", "hamzaban://reveal?tag=CC-4TQ8K;calc",
                    "hamzaban://app?agent=evil", "hamzaban://reveal?agent=codex", "hamzaban://resume?agent=codex", "hamzaban://app?tag=CC-4TQ8K&agent=codex",
                    "hamzaban://reveal?tag=CC-4TQ8K " + "x" * 200, "", None):
            with self.subTest(bad=bad), self.assertRaises(al.LinkError):
                al.parse_uri(bad)
        with self.assertRaises(ValueError):
            al.build_uri("format", tag="CC-4TQ8K")
        with self.assertRaises(ValueError):
            al.build_uri("reveal", tag="not a tag")
        with self.assertRaises(ValueError):
            al.build_uri("reveal")


class Where(unittest.TestCase):
    def test_codex_gets_the_exact_chat_link_built_from_the_thread_id(self):
        w = al.where(chat(), schemes={"codex"}, which=lambda c: None)
        self.assertEqual(w["primary"], {"href": f"codex://threads/{THREAD}", "label": "باز کردن همین چت در Codex", "exact": True})
        self.assertEqual(w["note"], "")
        self.assertEqual(w["resume"], {"cmd": f"codex resume {THREAD}", "available": False, "href": "hamzaban://resume?tag=CX-9ZZ99"})
        self.assertTrue(w["reveal"] and w["folder"])
        self.assertEqual((w["reveal_href"], w["folder_href"]), ("hamzaban://reveal?tag=CX-9ZZ99", "hamzaban://folder?tag=CX-9ZZ99"))

    def test_without_the_codex_link_registered_it_falls_back_to_opening_the_app(self):
        w = al.where(chat(), schemes=set(), which=lambda c: "/bin/codex")
        self.assertEqual((w["primary"]["href"], w["primary"]["exact"]), ("hamzaban://app?tag=CX-9ZZ99", False))
        self.assertTrue(w["resume"]["available"])

    def test_claude_desktop_opens_the_app_and_says_why_not_the_chat(self):
        w = al.where(chat(tag="CC-4TQ8K", agent="claude-code", entry="claude-desktop", source_id=CLAUDE_ID), schemes={"codex"}, which=lambda c: None)
        self.assertEqual((w["primary"]["href"], w["primary"]["exact"]), ("hamzaban://app?tag=CC-4TQ8K", False))
        self.assertIn("لینک مستقیم", w["note"])
        self.assertEqual(w["resume"]["cmd"], f"claude --resume {CLAUDE_ID}")

    def test_terminal_chats_have_no_application_to_open_only_the_command(self):
        w = al.where(chat(tag="CC-1AAAA", agent="claude-code", entry="cli", source_id=CLAUDE_ID), which=lambda c: "/usr/bin/claude")
        self.assertIsNone(w["primary"])
        self.assertTrue(w["resume"]["available"])
        self.assertIn("ترمینال", w["note"])

    def test_a_chat_known_only_from_a_closing_block_has_no_file_to_show(self):
        w = al.where(chat(tag="CA-2BBBB", agent="claude-ai", entry="hamzaban-close", source_id="hz-claude-ai-de62cf892222", cwd="",
                          path="20-Checkpoints/session-hz/20261104T163942Z-de62cf892222.md"))
        self.assertFalse(w["reveal"])
        self.assertFalse(w["folder"])
        self.assertIsNone(w["resume"])
        self.assertEqual(w["primary"]["href"], "hamzaban://app?tag=CA-2BBBB")

    def test_nothing_is_offered_for_a_bad_tag_or_an_unknown_agent(self):
        w = al.where(chat(tag="bad tag", agent="mystery", entry=""), schemes=set())
        self.assertIsNone(w["primary"])
        self.assertEqual((w["reveal_href"], w["folder_href"]), ("", ""))
        self.assertEqual(w["app_fa"], "Codex")           # the agent's own display name, since no application is known


class StartApps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hz-apps-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.object(al, "CACHE", self.tmp / "app-launchers.json"), mock.patch.object(al.os, "name", "nt")):
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, payload, calls):
        def run(cmd, **kw):
            calls.append(cmd)
            return type("R", (), {"stdout": payload.encode("utf-8"), "returncode": 0})()
        return run

    def test_the_list_is_read_once_and_remembered(self):
        calls = []
        payload = json.dumps([{"Name": "Claude", "AppID": "Claude_x!Claude"}, {"Name": "Chat On Steroids", "AppID": "electron.app.Chat On Steroids"}])
        run = self.fake_run(payload, calls)
        apps = al.start_apps(run=run)
        self.assertEqual(apps, {"Claude": "Claude_x!Claude", "Chat On Steroids": "electron.app.Chat On Steroids"})
        self.assertEqual(al.start_apps(run=run), apps)
        self.assertEqual(len(calls), 1, "the second look comes from the cache")
        al.start_apps(refresh=True, run=run)
        self.assertEqual(len(calls), 2)
        old = json.loads(al.CACHE.read_text(encoding="utf-8"))
        old["at"] -= al.CACHE_SECONDS + 5
        al.CACHE.write_text(json.dumps(old), encoding="utf-8")
        al.start_apps(run=run)
        self.assertEqual(len(calls), 3, "an old cache is read again")

    def test_a_single_app_comes_as_an_object_and_a_failure_changes_nothing(self):
        self.assertEqual(al.start_apps(run=self.fake_run(json.dumps({"Name": "Obsidian", "AppID": "md.obsidian"}), [])), {"Obsidian": "md.obsidian"})
        shutil.rmtree(self.tmp / "app-launchers.json", ignore_errors=True)
        al.CACHE.unlink()
        self.assertEqual(al.start_apps(run=self.fake_run("not json", [])), {})
        self.assertFalse(al.CACHE.exists(), "nothing is remembered from a failed look")

    def test_an_application_is_found_by_any_of_its_names(self):
        apps = {"ChatGPT": "OpenAI.Codex_x!App", "Claude": "Claude_x!Claude"}
        self.assertEqual(al.find_app("codex", apps), ("ChatGPT", "OpenAI.Codex_x!App"))
        self.assertEqual(al.find_app("claude", {"claude": "id"}), ("claude", "id"))
        self.assertIsNone(al.find_app("cos", apps))
        self.assertIsNone(al.find_app("claude-cli", apps))
        self.assertIsNone(al.find_app("nope", apps))

    def test_a_registered_scheme_is_looked_up_in_the_registry(self):
        class Reg:
            HKEY_CLASSES_ROOT = "HKCR"

            @staticmethod
            def OpenKey(root, name):
                if name != "codex":
                    raise OSError
                return type("K", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: False})()
        self.assertTrue(al.scheme_registered("codex", Reg))
        self.assertFalse(al.scheme_registered("nothing", Reg))


class Handle(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hz-handle-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "chat.jsonl"
        self.log.write_text("{}\n", encoding="utf-8")
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.rows = [chat(tag="CX-9ZZ99", path=str(self.log), cwd=str(self.work)),
                     chat(tag="CC-4TQ8K", agent="claude-code", entry="claude-desktop", source_id=CLAUDE_ID, path=str(self.log), cwd=str(self.work)),
                     chat(tag="CS-3CCCC", agent="cos", entry="chat-on-steroids", source_id="s1s1s1s1", path=str(self.tmp / "gone"), cwd=""),
                     chat(tag="CA-2BBBB", agent="claude-ai", entry="hamzaban-close", source_id="hz-1", path="x.md", cwd=""),
                     chat(tag="CC-EVIL1", agent="claude-code", entry="cli", source_id="x; calc", path=str(self.log), cwd=str(self.work))]
        self.reg = FakeRegistry(self.rows)
        self.popen, self.startfile = Recorder(), Recorder()
        for p in (mock.patch.object(al.sys, "platform", "win32"), mock.patch.object(al.os, "startfile", self.startfile, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def go(self, uri, which=lambda c: None, apps=None):
        return al.handle(uri, registry=self.reg, popen=self.popen, which=which, startfile=self.startfile, apps=apps if apps is not None else {})

    def test_reveal_selects_the_chats_file_in_explorer(self):
        self.go("hamzaban://reveal?tag=CX-9ZZ99")
        self.assertEqual(self.popen.calls, [((f'explorer.exe /select,"{self.log}"',), {})])

    def test_a_file_that_is_gone_or_was_never_there_is_a_message_not_a_crash(self):
        for tag in ("CS-3CCCC", "CA-2BBBB"):
            with self.subTest(tag=tag), self.assertRaises(al.LinkError):
                self.go(f"hamzaban://reveal?tag={tag}")
        self.assertEqual(self.popen.calls, [])

    def test_a_path_with_a_quote_in_it_is_refused(self):
        with self.assertRaises(al.LinkError):
            al.reveal_file(Path('C:\\a" & calc & "b'), popen=self.popen)
        self.assertEqual(self.popen.calls, [])

    def test_folder_opens_the_project_folder(self):
        self.go("hamzaban://folder?tag=CX-9ZZ99")
        self.assertEqual(self.startfile.calls, [((str(self.work),), {})])
        with self.assertRaises(al.LinkError):
            self.go("hamzaban://folder?tag=CS-3CCCC")

    def test_app_starts_the_application_by_its_start_menu_id_or_opens_its_website(self):
        self.go("hamzaban://app?tag=CC-4TQ8K", apps={"Claude": "Claude_x!Claude"})
        self.assertEqual(self.popen.calls[-1], ((["explorer.exe", "shell:AppsFolder\\Claude_x!Claude"],), {}))
        self.go("hamzaban://app?agent=codex", apps={"ChatGPT": "OpenAI.Codex_x!App"})
        self.assertEqual(self.popen.calls[-1], ((["explorer.exe", "shell:AppsFolder\\OpenAI.Codex_x!App"],), {}))
        self.go("hamzaban://app?tag=CA-2BBBB", apps={})                       # no Claude app here: its website
        self.assertEqual(self.startfile.calls[-1], (("https://claude.ai/recents",), {}))
        with self.assertRaises(al.LinkError):                                  # Chat On Steroids has no website and is not installed
            self.go("hamzaban://app?tag=CS-3CCCC", apps={})

    def test_resume_opens_a_terminal_in_the_project_folder_only_when_the_command_exists(self):
        with self.assertRaises(al.LinkError):
            self.go("hamzaban://resume?tag=CC-4TQ8K", which=lambda c: None)
        self.assertEqual(self.popen.calls, [])
        self.go("hamzaban://resume?tag=CC-4TQ8K", which=lambda c: r"C:\bin\x.exe")
        self.assertEqual(self.popen.calls, [((([r"C:\bin\x.exe", "-d", str(self.work), "powershell.exe", "-NoExit", "-Command", f"claude --resume {CLAUDE_ID}"]),), {})])

    def test_a_hostile_session_id_never_reaches_a_command(self):
        with self.assertRaises(al.LinkError):
            self.go("hamzaban://resume?tag=CC-EVIL1", which=lambda c: r"C:\bin\x.exe")
        self.assertEqual(self.popen.calls, [])

    def test_an_unknown_chat_or_a_missing_registry_is_a_message(self):
        with self.assertRaises(al.LinkError):
            self.go("hamzaban://reveal?tag=CC-ZZZZ9")

        class NoDb:
            def get(self, ref):
                raise FileNotFoundError("registry")
        with self.assertRaises(al.LinkError):
            al.handle("hamzaban://reveal?tag=CC-4TQ8K", registry=NoDb(), popen=self.popen)

    def test_the_handler_shows_a_message_instead_of_a_traceback(self):
        messages = []
        with mock.patch.object(al, "notify", messages.append):
            with mock.patch.object(al, "handle", side_effect=al.LinkError("برای این چت دستور ادامه‌ای وجود ندارد.")):
                self.assertEqual(al.handler_main(["hamzaban://resume?tag=CS-3CCCC"]), 1)
            with mock.patch.object(al, "handle", side_effect=RuntimeError("boom")):
                self.assertEqual(al.handler_main(["hamzaban://resume?tag=CS-3CCCC"]), 1)
            with mock.patch.object(al, "handle", return_value="ok"):
                self.assertEqual(al.handler_main(["hamzaban://resume?tag=CS-3CCCC"]), 0)
        self.assertEqual(len(messages), 2)
        self.assertIn("RuntimeError", messages[1])
        self.assertNotIn("boom", messages[1])


class TheWherePanel(unittest.TestCase):
    TOPICS = [{"tag": "CX-9ZZ99/T1", "n": 1, "title": "اولین بخش", "turns": 3, "first_ts": "2026-10-27T09:49:00Z", "last_ts": "2026-10-27T12:22:00Z",
               "first_user": "گزارش جامع انتقال پروژه را آماده کن لطفاً", "last_user": "x", "last_assistant": "بله، آماده شد", "from_n": 41, "to_n": 52,
               "current": False},
              {"tag": "CX-9ZZ99/T2", "n": 2, "title": "بخش دوم", "turns": 1, "first_ts": "", "last_ts": "", "first_user": "", "last_user": "",
               "last_assistant": "", "from_n": 53, "to_n": 53, "current": True}]

    def setUp(self):
        patch = mock.patch.object(al, "scheme_registered", lambda name, reg=None: True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_names_the_application_the_part_and_the_files(self):
        html = control_room.where_html(chat(topics=self.TOPICS))
        self.assertIn("برنامهٔ Codex", html)
        self.assertIn(f'href="codex://threads/{THREAD}"', html)
        self.assertIn("باز کردن همین چت در Codex", html)
        self.assertIn("پیام‌های شمارهٔ ۴۱ تا ۵۲ از ۷۱ پیامِ تو", html)
        self.assertIn("پیام شمارهٔ ۵۳ از ۷۱ پیامِ تو", html)
        self.assertIn('data-copy="گزارش جامع انتقال پروژه را آماده کن"', html)      # the first seven words: what to search for
        self.assertIn("Ctrl+F", html)
        self.assertIn('href="hamzaban://reveal?tag=CX-9ZZ99"', html)
        self.assertIn('href="hamzaban://folder?tag=CX-9ZZ99"', html)
        self.assertIn("C:\\work\\demo", html)
        self.assertIn('id="wt-CX-9ZZ99-2"', html)

    def test_nothing_from_a_chat_can_inject_markup(self):
        evil = "<img src=x onerror=alert(1)>"
        html = control_room.where_html(chat(title=evil, cwd='C:\\a"><script>x</script>', topics=[dict(self.TOPICS[0], title=evil, first_user=evil, last_assistant=evil)]))
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;img", html)

    def test_a_chat_without_topics_says_so_and_a_missing_module_removes_the_panel(self):
        self.assertIn("موضوع‌بندی نشده", control_room.where_html(chat()))
        card = control_room.chat_card(dict(chat(), state="idle", state_fa="ساکت", phrase="ادامه بده از CX-9ZZ99", reason="", reason_fa="", last_user="hi",
                                           parent="", continued_by="", open_tasks=[], resume="", live=False, last_user_ts="", cut_at="", cut_detail="",
                                           reset_at="", topics=self.TOPICS))
        self.assertIn('data-where="CX-9ZZ99"', card)
        self.assertIn('<template id="w-CX-9ZZ99">', card)
        self.assertIn('data-where-topic="1"', card)
        with mock.patch.object(control_room, "app_links", None):
            self.assertEqual(control_room.where_html(chat()), "")
            bare = control_room.chat_card(dict(chat(), state="idle", state_fa="ساکت", phrase="p", reason="", reason_fa="", last_user="hi", parent="",
                                               continued_by="", open_tasks=[], resume="", live=False, last_user_ts="", cut_at="", cut_detail="",
                                               reset_at="", topics=[]))
        self.assertNotIn("data-where", bare)
        self.assertNotIn("<template", bare)

    def test_the_page_has_the_dialog_the_identity_and_a_working_favicon(self):
        self.assertIn('<dialog id="where-dlg"', control_room.PAGE)
        self.assertIn("<title>همزبان · اتاق کنترل</title>", control_room.PAGE)
        self.assertIn('<link rel="icon" type="image/svg+xml" href="{favicon}">', control_room.PAGE)
        svg = base64.b64decode(control_room.FAVICON.split(",", 1)[1]).decode("utf-8")
        self.assertTrue(svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"'))
        self.assertIn("#22c7b8", svg)
        self.assertIn("$('dialog[open]')", control_room.JS)           # an open panel keeps the page from reloading under the reader


class SnapshotFields(unittest.TestCase):
    """The registry's snapshot carries what the panel needs: the application entry, the id, the log path and where each part sits."""

    def test_entry_id_path_and_the_numbers_of_each_topics_messages(self):
        tmp = Path(tempfile.mkdtemp(prefix="hz-snap-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".ai-memory").mkdir()
        con = chats.Registry(tmp, sources=[]).connect(create=True)
        topics = json.dumps({"topics": [{"n": 1, "title": "A", "turns": 2}, {"n": 2, "title": "B", "turns": 1}], "cur": 2})
        con.execute("INSERT INTO chats(tag, agent, source_id, kind, entry, cwd, project, title, first_ts, last_ts, user_turns, state, path, topics) "
                    "VALUES('CC-4TQ8K','claude-code',?,'main','claude-desktop',?,'demo','T','2026-11-05T08:00:00Z','2026-11-05T09:00:00Z',3,'idle',?,?)",
                    (CLAUDE_ID, r"C:\work\demo", r"C:\Users\u\.claude\projects\p\s.jsonl", topics))
        for seq, role, topic in ((1, "user", 1), (2, "assistant", 1), (3, "user", 1), (4, "assistant", 1), (5, "user", 2), (6, "assistant", 2)):
            con.execute("INSERT INTO msgs(chat, seq, ts, role, text, topic) VALUES('CC-4TQ8K', ?, '2026-11-05T08:00:00Z', ?, ?, ?)", (seq, role, f"{role} {seq}", topic))
        con.commit()
        con.close()
        snap = chats.Registry(tmp, sources=[]).snapshot()
        c = snap["chats"][0]
        self.assertEqual((c["entry"], c["source_id"], c["path"], c["cwd"]), ("claude-desktop", CLAUDE_ID, r"C:\Users\u\.claude\projects\p\s.jsonl", r"C:\work\demo"))
        self.assertEqual([(t["tag"], t["from_n"], t["to_n"]) for t in c["topics"]], [("CC-4TQ8K/T1", 1, 2), ("CC-4TQ8K/T2", 3, 3)])


class Hygiene(unittest.TestCase):
    def test_the_new_modules_are_offline_stdlib_only_and_python_310(self):
        import ast
        for name in ("app_links.py", "app_window.py", "shortcuts.py", "hamzaban_app.pyw", "hamzaban_open.pyw"):
            text = (SRC / name).read_text(encoding="utf-8")
            ast.parse(text, feature_version=(3, 10))
            for banned in ("import socket", "urllib", "http.client", "import requests", "os.system", "eval(", "exec(", "pickle", "shell=True"):
                self.assertNotIn(banned, text, f"{name}: {banned}")


if __name__ == "__main__":
    unittest.main()

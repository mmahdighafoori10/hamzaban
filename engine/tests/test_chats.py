"""Chat registry: tags, topic sub-tags, reading each agent's log, telling a cut-off chat from a finished one, resuming by tag.

Fixtures follow the real formats (Claude Code transcript JSONL, Codex rollout JSONL, Chat On Steroids session folders,
a hamzaban closing checkpoint). Everything runs in temp dirs through SUITE_USER_HOME / SUITE_APPDATA.
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import chat_sources  # noqa: E402
import chat_topics  # noqa: E402
import chats  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
SID = "11111111-2222-3333-4444-555555555555"


def T(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jl(path: Path, rows: list, minutes_ago: float = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    stamp = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(path, (stamp, stamp))


# ---- Claude Code rows
def cc_user(text, ago, sid=SID, cwd=r"C:\Users\x\proj\demo"):
    return {"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": T(ago), "message": {"role": "user", "content": text}}


def cc_result(tool_id, ago):
    return {"type": "user", "timestamp": T(ago), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def cc_asst(text, ago, stop="end_turn"):
    return {"type": "assistant", "timestamp": T(ago), "message": {"role": "assistant", "stop_reason": stop, "content": [{"type": "text", "text": text}]}}


def cc_tool(tool_id, name, inp, ago):
    return {"type": "assistant", "timestamp": T(ago), "message": {"role": "assistant", "stop_reason": "tool_use",
                                                                   "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


# ---- Codex rows
def cx(kind, payload, ago, typ="event_msg"):
    return {"timestamp": T(ago), "type": typ, "payload": dict(payload, **({"type": kind} if kind else {}))}


def cx_user(text, ago):
    return cx("item_completed", {"item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}, ago)


def cx_agent(text, ago, phase="final_answer"):
    return cx("item_completed", {"item": {"type": "AgentMessage", "phase": phase, "content": [{"type": "text", "text": text}]}}, ago)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chats-test-"))
        self.vault = self.tmp / "vault"
        (self.vault / ".ai-memory").mkdir(parents=True)
        self.home, self.appdata = self.tmp / "home", self.tmp / "appdata"
        self.home.mkdir()
        self.appdata.mkdir()
        patch = mock.patch.dict(os.environ, {"SUITE_USER_HOME": str(self.home), "SUITE_APPDATA": str(self.appdata)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reg = chats.Registry(self.vault)

    def cc_file(self, rows, sid=SID, ago=1):
        p = self.home / ".claude" / "projects" / "C--proj" / f"{sid}.jsonl"
        jl(p, rows, ago)
        return p

    def cx_file(self, rows, uuid="aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb", ago=1, source="vscode"):
        p = self.home / ".codex" / "sessions" / "2026" / "11" / "05" / f"rollout-2026-11-05T10-00-00-{uuid}.jsonl"
        meta = {"timestamp": T(ago + 30), "type": "session_meta", "payload": {"id": uuid, "cwd": r"C:\Users\x\codex-proj", "source": source}}
        jl(p, [meta] + rows, ago)
        return uuid

    def scan(self, **kw):
        stats = self.reg.scan(now=NOW, **kw)
        self.assertEqual(stats["errors"], [])
        return stats

    def chat(self, agent, sid):
        return self.reg.get(chats.candidate_tag(agent, sid))


class TagTests(Base):
    def test_tag_is_stable_short_and_prefixed_by_agent(self):
        a = chats.candidate_tag("claude-code", SID)
        self.assertRegex(a, r"^CC-[0-9A-HJKMNP-TV-Z]{5}$")
        self.assertEqual(a, chats.candidate_tag("claude-code", SID))
        self.assertNotEqual(a, chats.candidate_tag("codex", SID))
        self.assertTrue(chats.candidate_tag("codex", SID).startswith("CX-"))
        self.assertTrue(chats.candidate_tag("claude-ai", SID).startswith("CA-"))
        self.assertTrue(chats.candidate_tag("something-new", SID).startswith("XX-"))

    def test_references_in_any_case_and_with_a_topic(self):
        self.assertEqual(chats.parse_ref("ادامه بده از cc-7k3qf لطفا"), ("CC-7K3QF", None))
        self.assertEqual(chats.parse_ref("CC-7K3QF/T3"), ("CC-7K3QF", 3))
        self.assertEqual(chats.parse_ref("see cx-0a1b2.t12 now"), ("CX-0A1B2", 12))
        for bad in ("hello", "ABC-12345", "CC-12", "ZZ-12345", "CC-7K3QF7K3QF7K3QF"):
            self.assertIsNone(chats.parse_ref(bad), bad)
        self.assertTrue(chats.is_ref("CC-7K3QF/T2"))
        self.assertFalse(chats.is_ref("continue the CC-7K3QF chat and then do much more work please"))

    def test_a_tag_that_collides_with_another_chat_gets_a_longer_one(self):
        con = self.reg.connect()
        mine = chats.candidate_tag("codex", "x-source-1")
        con.execute("INSERT INTO chats(tag, agent, source_id) VALUES (?,?,?)", (mine, "codex", "some-other-chat"))
        con.commit()
        got = self.reg._alloc_tag(con, "codex", "x-source-1")
        con.close()
        self.assertNotEqual(got, mine)
        self.assertTrue(got.startswith(mine))
        self.assertEqual(len(got), len(mine) + 1)


class TopicTests(unittest.TestCase):
    def test_a_hashtag_names_the_topic_and_is_found_again(self):
        st = chat_topics.TopicState()
        self.assertEqual(st.add("#سئو بریم سراغ ساختار سایت و کلمات کلیدی"), 1)
        self.assertEqual(st.add("#انتشار حالا باید روی سرور بگذاریمش و دامنه بخریم"), 2)
        self.assertEqual(st.add("یه چیز دیگه برای #سئو هم هست که جا افتاد، عنوان صفحه‌ها"), 1)
        self.assertEqual([t["title"] for t in st.topics], ["سئو", "انتشار"])
        self.assertEqual({t["source"] for t in st.topics}, {"declared"})

    def test_short_replies_stay_in_the_current_topic(self):
        st = chat_topics.TopicState()
        n = st.add("لطفا ساختار پوشه‌های پروژه و فایل‌های پیکربندی دیتابیس را بررسی کن و بگو چه مشکلی دارد")
        for reply in ("بله", "ادامه بده", "همونو انجام بده", "ok"):
            self.assertEqual(st.add(reply), n)

    def test_related_followups_stay_and_an_unrelated_request_opens_a_new_topic(self):
        st = chat_topics.TopicState()
        a = st.add("ساختار پوشه‌های پروژه و فایل‌های پیکربندی دیتابیس را بررسی کن و بگو چه مشکلی دارد در اتصال")
        b = st.add("مشکل اتصال دیتابیس هنوز هست و فایل پیکربندی را هم باید اصلاح کنیم تا خطا برطرف شود")
        c = st.add("حالا برای فروشگاه یک طراحی کاملا جدید با رنگ‌های گرم و فونت‌های خوانا و آیکن‌های ساده لازم دارم")
        self.assertEqual((a, b), (1, 1))
        self.assertEqual(c, 2)
        # coming back to the first subject finds it again while it is still among the recent topics
        d = st.add("برگردیم به اتصال دیتابیس: فایل پیکربندی اصلاح شد ولی هنوز خطا برطرف نمی‌شود و مشکل اتصال می‌ماند")
        self.assertEqual(d, 1)

    def test_assistant_words_let_a_short_followup_find_its_topic(self):
        st = chat_topics.TopicState()
        st.add("مقایسه‌ی چند کتابخانه برای ساخت نمودار روی صفحه داشبورد را انجام بده و بگو کدام بهتر است")
        st.note_assistant("پیشنهاد من کتابخانه‌ی نمودار سبک است؛ دو گزینه: ECharts و Chart.js برای داشبورد")
        self.assertEqual(st.add("Chart.js"), 1)

    def test_numbers_are_stable_and_state_survives_json(self):
        msgs = ["بررسی ساختار پروژه و پوشه‌ها برای مهاجرت دیتابیس به نسخه‌ی جدید و بررسی اسکریپت‌های قدیمی انجام شود",
                "بله", "ساخت یک صفحه‌ی فرود با تصاویر و متن‌های بازاریابی و فرم تماس و رنگ‌های برند جدید لازم است",
                "ادامه بده", "بازگشت به اسکریپت‌های دیتابیس: مهاجرت نسخه‌ی جدید خطا می‌دهد و بررسی ساختار لازم است"]
        whole = chat_topics.TopicState()
        batch = [whole.add(m) for m in msgs]
        part = chat_topics.TopicState()
        step = [part.add(m) for m in msgs[:2]]
        part = chat_topics.TopicState(json.loads(json.dumps(part.to_json())))   # a registry stores it as JSON between passes
        step += [part.add(m) for m in msgs[2:]]
        self.assertEqual(batch, step)
        self.assertEqual(batch, [1, 1, 2, 2, 1])

    def test_titles_are_clean_first_sentences(self):
        self.assertEqual(chat_topics.title_of('<pasted_content id="x"> سلام. لطفا این را بررسی کن'), "سلام. لطفا این را بررسی کن")
        self.assertLessEqual(len(chat_topics.title_of("کلمه " * 80)), 62)
        self.assertEqual(chat_topics.title_of("   "), "(بدون عنوان)")


class ClaudeCodeReader(Base):
    def read(self, rows, cursor=0, scratch=None):
        p = self.cc_file(rows)
        info = {"source_id": SID, "path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        return chat_sources.ClaudeCode().read(info, cursor, scratch or {})

    def test_user_assistant_tools_plan_and_title(self):
        ev, cur, sc = self.read([
            cc_user("سلام، لطفا فایل را درست کن", 30),
            {"type": "custom-title", "customTitle": "رفع باگ فایل"},
            cc_tool("t1", "Edit", {"file_path": r"C:\x\a.py"}, 29),
            cc_result("t1", 28),
            cc_tool("t2", "TodoWrite", {"todos": [{"content": "اول", "status": "completed"}, {"content": "دوم", "status": "pending"}]}, 27),
            cc_asst("انجام شد", 26),
        ])
        kinds = [e["k"] for e in ev]
        self.assertIn("user", kinds)
        self.assertEqual([e["text"] for e in ev if e["k"] == "user"], ["سلام، لطفا فایل را درست کن"])
        self.assertEqual([e["title"] for e in ev if e["k"] == "meta" and e.get("title")], ["رفع باگ فایل"])
        self.assertEqual([e["target"] for e in ev if e["k"] == "tool"], [r"C:\x\a.py"])
        plan = [e for e in ev if e["k"] == "plan"][-1]["items"]
        self.assertEqual([p["status"] for p in plan], ["completed", "pending"])
        self.assertEqual(sc["open_tools"], ["t2"], "t1 was answered, t2 (TodoWrite) never got a result")
        self.assertTrue(any(e["k"] == "assistant" and e["final"] for e in ev))

    def test_midturn_messages_system_noise_and_sidechains(self):
        ev, *_ = self.read([
            cc_user("<system-reminder>ignore me</system-reminder>", 20),
            dict(cc_user("sub agent chatter", 19), isSidechain=True),
            dict(cc_user("meta row", 18), isMeta=True),
            {"type": "attachment", "timestamp": T(17), "attachment": {"type": "queued_command", "prompt": "پیام وسط کار", "commandMode": "prompt"}},
            {"type": "attachment", "timestamp": T(16), "attachment": {"type": "queued_command", "prompt": "<task-notification>done</task-notification>", "commandMode": "task-notification"}},
            cc_user("[Request interrupted by user]", 15),
            cc_user("پاسخ قبلی متوقف شده\nحالا فایل دوم را بساز", 14),
        ])
        self.assertEqual([e["text"] for e in ev if e["k"] == "user"], ["پیام وسط کار", "حالا فایل دوم را بساز"])
        self.assertEqual(sum(1 for e in ev if e["k"] == "mark" and e["name"] == "turn_aborted"), 2)

    def test_an_api_error_is_a_limit_mark_not_an_assistant_message(self):
        row = dict(cc_asst("You've hit your limit · resets 5pm", 5, stop="stop_sequence"), isApiErrorMessage=True)
        ev, *_ = self.read([cc_user("go", 10), row])
        self.assertEqual([e["name"] for e in ev if e["k"] == "mark"], ["limit"])
        self.assertFalse([e for e in ev if e["k"] == "assistant"])
        ev, *_ = self.read([cc_user("go", 10), cc_asst("cut off mid", 5, stop="max_tokens")])
        self.assertEqual([e["name"] for e in ev if e["k"] == "mark"], ["limit"])

    def test_reading_in_two_pieces_equals_reading_once(self):
        rows = [cc_user("یک", 9), cc_asst("پاسخ یک", 8), cc_user("دو", 7), cc_asst("پاسخ دو", 6)]
        whole, cursor, _ = self.read(rows)
        p = self.cc_file(rows[:2])
        info = {"source_id": SID, "path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        first, c1, sc = chat_sources.ClaudeCode().read(info, 0, {})
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            for r in rows[2:]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        info["size"] = p.stat().st_size
        second, c2, _ = chat_sources.ClaudeCode().read(info, c1, sc)
        self.assertEqual((first + second), whole)
        self.assertEqual(c2, cursor)

    def test_a_half_written_last_line_waits(self):
        p = self.cc_file([cc_user("کامل", 5)])
        with p.open("ab") as fh:
            fh.write(b'{"type": "user", "message": {"role": "us')
        info = {"source_id": SID, "path": str(p), "size": p.stat().st_size, "mtime": p.stat().st_mtime}
        ev, cur, _ = chat_sources.ClaudeCode().read(info, 0, {})
        self.assertEqual(len([e for e in ev if e["k"] == "user"]), 1)
        self.assertLess(cur, p.stat().st_size)


class CodexReader(Base):
    def read(self, rows):
        uuid = self.cx_file(rows)
        info = [i for i in chat_sources.Codex().discover(0) if i["source_id"].endswith(uuid)][0]
        return chat_sources.Codex().read(info, 0, {})

    def test_messages_markers_and_filters(self):
        ev, *_ = self.read([
            cx("task_started", {}, 40),
            cx_user("# AGENTS.md instructions for x\n\nbe nice", 39),
            cx_user("# Files mentioned by the user:\n\n## a.pdf: C:\\a.pdf\n\n## My request for Codex:\nاین فایل را خلاصه کن", 38),
            cx_user('{"questionItemId": "x", "answers": []}', 37),
            cx_user("# Files mentioned by the user:\n\n## b.pdf: C:\\b.pdf", 36),
            cx_agent("در حال بررسی", 35, phase="commentary"),
            cx_agent("خلاصه آماده است", 34),
            cx("command", {}, 33, typ="response_item"),
            cx("task_complete", {"last_agent_message": "خلاصه آماده است"}, 32),
            cx_user("بعدی", 31),
            cx("turn_aborted", {"reason": "interrupted"}, 30),
        ])
        self.assertEqual([e["text"] for e in ev if e["k"] == "user"], ["این فایل را خلاصه کن", "بعدی"])
        self.assertEqual([e["final"] for e in ev if e["k"] == "assistant"], [False, True], "the final answer is not repeated by task_complete")
        self.assertEqual([e["name"] for e in ev if e["k"] == "mark"], ["turn_start", "turn_done", "turn_aborted"])
        meta = [e for e in ev if e["k"] == "meta"][0]
        self.assertEqual((meta["kind"], meta["cwd"]), ("main", r"C:\Users\x\codex-proj"))

    def test_rate_limit_only_counts_when_reached(self):
        ev, *_ = self.read([cx("token_count", {"rate_limits": {"rate_limit_reached_type": None}}, 5),
                            cx("token_count", {"rate_limits": {"rate_limit_reached_type": "primary"}}, 4)])
        self.assertEqual([(e["name"], e["detail"]) for e in ev if e["k"] == "mark"], [("limit", "primary")])

    def test_subagent_sessions_are_marked(self):
        self.cx_file([cx("task_started", {}, 5)], uuid="bbbbbbbb-1111-2222-3333-cccccccccccc", source={"subagent": {"other": "guardian"}}) if False else None
        uuid = "dddddddd-1111-2222-3333-eeeeeeeeeeee"
        p = self.home / ".codex" / "sessions" / "2026" / "11" / "05" / f"rollout-2026-11-05T09-00-00-{uuid}.jsonl"
        jl(p, [{"timestamp": T(20), "type": "session_meta", "payload": {"id": uuid, "cwd": "C:\\x", "source": {"subagent": {"other": "guardian"}}}}], 5)
        info = chat_sources.Codex().discover(0)[0]
        ev, *_ = chat_sources.Codex().read(info, 0, {})
        self.assertEqual([e["kind"] for e in ev if e["k"] == "meta"], ["subagent"])


class RegistryTests(Base):
    def test_scan_registers_every_agent_and_is_idempotent(self):
        self.cc_file([cc_user("بررسی ساختار پروژه و فایل‌های پیکربندی", 50), cc_asst("انجام شد", 49)], ago=49)
        self.cx_file([cx("task_started", {}, 40), cx_user("یک درخواست برای کدکس", 39), cx_agent("پاسخ", 38), cx("task_complete", {}, 37)], ago=37)
        s = self.scan()
        self.assertEqual((s["seen"], s["ingested"], s["chats"]), (2, 2, 2))
        self.assertEqual(self.chat("claude-code", SID)["user_turns"], 1)
        again = self.scan()
        self.assertEqual(again["ingested"], 0, "nothing changed: nothing re-read")

    def test_an_appended_chat_grows_and_keeps_its_topics_and_tag(self):
        rows = [cc_user("بررسی ساختار پروژه و فایل‌های پیکربندی دیتابیس برای مهاجرت", 100), cc_asst("بررسی شد", 99)]
        p = self.cc_file(rows, ago=99)
        self.scan()
        before = self.chat("claude-code", SID)
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            for r in (cc_user("ساخت صفحه‌ی فرود جدید با رنگ‌های برند و تصاویر بازاریابی و فرم تماس مشتریان", 20), cc_asst("ساخته شد", 19)):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.utime(p, ((NOW - timedelta(minutes=19)).timestamp(),) * 2)
        self.scan()
        after = self.chat("claude-code", SID)
        self.assertEqual(after["tag"], before["tag"])
        self.assertEqual(after["user_turns"], 2)
        self.assertEqual([t["tag"] for t in after["topic_list"]], [f"{before['tag']}/T1", f"{before['tag']}/T2"])
        self.assertEqual(after["topic_list"][0]["title"], before["topic_list"][0]["title"])

    def test_states_active_idle_and_the_ways_a_chat_gets_cut_off(self):
        def state(sid, rows, ago, agent="claude-code"):
            self.cc_file(rows, sid=sid, ago=ago)
            self.scan()
            c = self.chat(agent, sid)
            return c["state"], c["reason"]
        s = lambda n: f"{n:08d}-2222-3333-4444-555555555555"  # noqa: E731
        self.assertEqual(state(s(1), [cc_user("درخواست", 3), cc_asst("پاسخ", 2)], 2), ("active", ""))
        self.assertEqual(state(s(2), [cc_user("درخواست", 300), cc_asst("پاسخ", 299)], 299), ("idle", ""))
        self.assertEqual(state(s(3), [cc_user("درخواست", 300), cc_asst("پاسخ", 299), cc_user("ولی هنوز این را هم بگو", 298)], 298), ("interrupted", "unanswered"))
        self.assertEqual(state(s(4), [cc_user("درخواست", 300), cc_tool("t9", "Bash", {"command": "x"}, 299)], 299), ("interrupted", "tool"))
        self.assertEqual(state(s(5), [cc_user("درخواست", 300), dict(cc_asst("You've hit your limit", 299), isApiErrorMessage=True)], 299), ("interrupted", "limit"))
        self.assertEqual(state(s(6), [cc_user("درخواست", 300), dict(cc_asst("You've hit your limit", 299), isApiErrorMessage=True), cc_user("حالا ادامه بده", 250), cc_asst("باشه", 249)], 249), ("idle", ""))

    def test_codex_turn_open_aborted_and_complete(self):
        def run(uuid, rows, ago):
            self.cx_file(rows, uuid=uuid, ago=ago)
            self.scan()
            c = self.chat("codex", uuid)
            return c["state"], c["reason"]
        base = lambda ago: [cx("task_started", {}, ago + 3), cx_user("درخواست", ago + 2), cx_agent("پاسخ", ago + 1)]  # noqa: E731
        self.assertEqual(run("00000001-1111-2222-3333-444444444444", base(400) + [cx("task_complete", {}, 399)], 399), ("idle", ""))
        self.assertEqual(run("00000002-1111-2222-3333-444444444444", base(400), 400), ("interrupted", "turn"))
        self.assertEqual(run("00000003-1111-2222-3333-444444444444", base(400) + [cx("turn_aborted", {"reason": "interrupted"}, 399)], 399), ("interrupted", "aborted"))
        self.assertEqual(run("00000004-1111-2222-3333-444444444444", base(400) + [cx("token_count", {"rate_limits": {"rate_limit_reached_type": "primary"}}, 399)], 399), ("interrupted", "limit"))

    def test_a_chat_that_starts_by_naming_an_old_one_continues_it(self):
        self.cc_file([cc_user("کار مهم", 600), cc_asst("نیمه", 599), cc_user("ادامه بده", 598)], sid=SID, ago=598)
        self.scan()
        old = self.chat("claude-code", SID)
        self.assertEqual(old["state"], "interrupted")
        new_sid = "99999999-2222-3333-4444-555555555555"
        self.cc_file([cc_user(f"ادامه بده از {old['tag']} لطفا", 30), cc_asst("باشه", 29)], sid=new_sid, ago=29)
        self.scan()
        new, old = self.chat("claude-code", new_sid), self.chat("claude-code", SID)
        self.assertEqual(new["parent"], old["tag"])
        self.assertEqual((old["state"], old["continued_by"]), ("continued", new["tag"]))

    def test_secrets_never_reach_the_registry(self):
        self.cc_file([cc_user("my key is sk-abcdefghijklmnop1234567890 and password = hunter2supersecret", 5), cc_asst("noted", 4)], ago=4)
        self.scan()
        con = sqlite3.connect(self.reg.db_path)
        blob = " ".join(str(v) for row in con.execute("SELECT * FROM msgs") for v in row) + " ".join(str(v) for row in con.execute("SELECT * FROM chats") for v in row)
        con.close()
        self.assertNotIn("sk-abcdefghijklmnop1234567890", blob)
        self.assertNotIn("hunter2supersecret", blob)

    def test_a_chat_still_being_read_is_not_called_cut_off(self):
        self.cc_file([cc_user("درخواست", 300), cc_asst("پاسخ", 299), cc_user("آخرین", 298)], ago=298)
        self.scan()
        con = self.reg.connect()
        con.execute("UPDATE chats SET scratch=json_set(scratch, '$.incomplete', json('true'))")
        con.commit()
        con.close()
        con = self.reg.connect()
        with con:
            self.reg._reclassify(con, NOW)
        row = con.execute("SELECT state FROM chats").fetchone()
        con.close()
        self.assertEqual(row["state"], "idle")


class CutOffTests(Base):
    """The cut that really happened on 2026-11-04: Codex ended its turn with task_complete.error = usage_limit_exceeded while the
    log was still fresh. Such a chat must show as cut off at once, with the time its tokens come back; a chat that only had a
    hiccup (an error followed by more work) must not."""

    LIMIT_MSG = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage "
                 "to purchase more credits or try again at 2:32 AM.")

    @staticmethod
    def tokens(pct, ago, resets_at):
        win = {"used_percent": pct, "window_minutes": 300, "resets_at": resets_at}
        week = {"used_percent": 16.0, "window_minutes": 10080, "resets_at": resets_at + 5 * 86400}
        return cx("token_count", {"rate_limits": {"limit_id": "codex", "primary": win, "secondary": week, "rate_limit_reached_type": None}}, ago)

    def limit_end(self, ago, resets_at):
        """The last rows of that chat, as Codex wrote them: the window fills up, the turn ends with the error, an empty count follows."""
        return [self.tokens(99.0, ago + 2, resets_at), self.tokens(100.0, ago + 1, resets_at),
                cx("task_complete", {"last_agent_message": None,
                                     "error": {"message": self.LIMIT_MSG, "codex_error_info": "usage_limit_exceeded"}}, ago),
                cx("token_count", {"rate_limits": {"limit_id": "premium", "primary": None, "secondary": None, "rate_limit_reached_type": None}}, ago)]

    def test_a_usage_limit_cut_shows_at_once_with_the_time_the_tokens_return(self):
        reset = int((NOW + timedelta(hours=2)).timestamp())
        uuid = self.cx_file([cx("task_started", {}, 30), cx_user("ادامه بده", 29), cx_agent("در حال کار", 20, phase="commentary")]
                            + self.limit_end(1, reset), ago=1)
        self.scan()
        c = self.chat("codex", uuid)
        self.assertEqual((c["state"], c["reason"]), ("interrupted", "limit"), "written a minute ago, yet already a cut-off chat")
        item = next(i for i in self.reg.snapshot(now=NOW)["chats"] if i["tag"] == c["tag"])
        self.assertEqual(item["reset_at"], (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(item["cut_at"][:19], T(1)[:19])
        self.assertIn("usage limit", item["cut_detail"])
        self.assertFalse(item["live"])
        self.assertIn("usage window opens again", self.reg.resume_text(c["tag"]))

    def test_another_error_may_be_retried_so_it_counts_after_a_short_wait(self):
        def run(uuid, error_ago):
            err = {"message": 'unexpected status 404 Not Found: {"detail":"Not Found"}', "codex_error_info": "other"}
            self.cx_file([cx("task_started", {}, error_ago + 5), cx_user("درخواست", error_ago + 4), cx("task_complete", {"error": err}, error_ago)],
                         uuid=uuid, ago=error_ago)
            self.scan()
            c = self.chat("codex", uuid)
            return c["state"], c["reason"]
        self.assertEqual(run("0000000a-1111-2222-3333-444444444444", 0.5), ("active", ""))
        self.assertEqual(run("0000000b-1111-2222-3333-444444444444", 5), ("interrupted", "error"))

    def test_a_chat_that_went_on_after_the_cut_is_not_cut_off(self):
        reset = int((NOW - timedelta(hours=1)).timestamp())
        uuid = self.cx_file([cx("task_started", {}, 600), cx_user("درخواست", 599)] + self.limit_end(590, reset)
                            + [cx("task_started", {}, 300), cx_user("حالا ادامه بده", 299), cx_agent("انجام شد", 298),
                               cx("task_complete", {"last_agent_message": "انجام شد"}, 297)], ago=297)
        self.scan()
        c = self.chat("codex", uuid)
        self.assertEqual((c["state"], c["reason"]), ("idle", ""))

    def test_a_used_up_window_alone_is_not_a_cut(self):
        uuid = self.cx_file([cx("task_started", {}, 30), cx_user("درخواست", 29), self.tokens(100.0, 28, int((NOW + timedelta(hours=1)).timestamp())),
                             cx_agent("آخرین کار را تمام کردم", 27), cx("task_complete", {"last_agent_message": "آخرین کار را تمام کردم"}, 26)], ago=26)
        self.scan()
        self.assertEqual(self.chat("codex", uuid)["state"], "idle")

    def test_claude_code_usage_limit_shows_at_once(self):
        self.cc_file([cc_user("درخواست", 10), dict(cc_asst("You've hit your limit · resets 3am", 1), isApiErrorMessage=True)], ago=1)
        self.scan()
        c = self.chat("claude-code", SID)
        self.assertEqual((c["state"], c["reason"]), ("interrupted", "limit"))

    def test_claude_code_error_followed_by_work_is_only_a_hiccup(self):
        err = {"type": "system", "level": "error", "subtype": "api_error", "timestamp": T(6), "content": "API Error: Connection lost mid-response"}
        # the error is older than the grace period, then the agent carries on with a tool call that has no text of its own
        self.cc_file([cc_user("درخواست", 20), err, cc_tool("tq", "Bash", {"command": "ls"}, 5)], ago=1)
        self.scan()
        c = self.chat("claude-code", SID)
        self.assertEqual((c["state"], c["reason"]), ("active", ""))
        item = next(i for i in self.reg.snapshot(now=NOW)["chats"] if i["tag"] == c["tag"])
        self.assertTrue(item["live"], "an open tool call and a log written a minute ago: it is working right now")


class ResumeAndNotes(Base):
    def setUp(self):
        super().setUp()
        rows = [cc_user("بررسی ساختار پروژه و فایل‌های پیکربندی دیتابیس برای مهاجرت نسخه‌ی جدید", 700), cc_asst("ساختار بررسی شد و سه مشکل پیدا شد", 699),
                cc_user("ساخت صفحه‌ی فرود با تصاویر بازاریابی و فرم تماس مشتریان و رنگ‌های برند جدید", 690), cc_asst("صفحه ساخته شد", 689),
                cc_tool("tp", "TodoWrite", {"todos": [{"content": "تست فرم", "status": "pending"}, {"content": "انتشار", "status": "completed"}]}, 688),
                cc_result("tp", 687.5),
                cc_tool("te", "Edit", {"file_path": r"C:\x\landing.html"}, 687), cc_result("te", 686),
                cc_user("فرم تماس را هم تست کن و بگو کار می‌کند", 685)]
        self.cc_file(rows, ago=685)
        self.scan()
        self.chat0 = self.chat("claude-code", SID)
        self.tag = self.chat0["tag"]

    def test_resume_names_the_state_topics_last_exchange_tasks_and_files(self):
        text = self.reg.resume_text(self.tag)
        self.assertIn(self.tag, text)
        self.assertIn("نیمه‌کاره", text)
        self.assertIn("بی‌پاسخ", text)
        self.assertIn(f"{self.tag}/T1", text)
        self.assertIn(f"{self.tag}/T2", text)
        self.assertIn("فرم تماس را هم تست کن", text)
        self.assertIn("تست فرم", text)
        self.assertNotIn("انتشار", text.split("OPEN TASKS")[1].split("FILES")[0], "a completed task is not open")
        self.assertIn("landing.html", text)

    def test_resume_of_one_topic_shows_only_that_topic(self):
        text = self.reg.resume_text(f"{self.tag}/T1")
        self.assertIn("FOCUS", text)
        self.assertIn("سه مشکل پیدا شد", text)
        self.assertNotIn("صفحه ساخته شد", text)
        self.assertIn(f"{self.tag}/T2", text, "the other topics are listed so the person can pick another")

    def test_resume_errors_are_clear_and_the_size_is_capped(self):
        with self.assertRaises(ValueError):
            self.reg.resume_text("hello")
        with self.assertRaises(ValueError):
            self.reg.resume_text("CC-00000")
        with self.assertRaises(ValueError):
            self.reg.resume_text(f"{self.tag}/T9")
        self.assertLessEqual(len(self.reg.resume_text(self.tag, 700)), 700)

    def test_notes_carry_obsidian_tags_for_the_chat_and_every_topic_and_are_not_rewritten_needlessly(self):
        note = self.vault / "60-Chats" / f"{self.tag}.md"
        self.assertTrue(note.is_file())
        text = note.read_text(encoding="utf-8")
        head = text.split("---", 2)[1]
        for t in (f"chat/{self.tag}", f"chat/{self.tag}/T1", f"chat/{self.tag}/T2", "agent/claude-code", "state/interrupted"):
            self.assertIn(t, head)
        self.assertIn(f"#chat/{self.tag}/T2", text)
        self.assertIn("ادامه بده از", text)
        stamp = note.stat().st_mtime_ns
        self.scan()
        self.assertEqual(note.stat().st_mtime_ns, stamp, "unchanged chat: unchanged note")

    def test_snapshot_is_plain_data_for_the_interface(self):
        snap = self.reg.snapshot(NOW)
        self.assertEqual(snap["counts"]["interrupted"], 1)
        item = snap["chats"][0]
        self.assertEqual((item["tag"], item["state"], item["phrase"]), (self.tag, "interrupted", f"ادامه بده از {self.tag}"))
        self.assertEqual(len(item["topics"]), 2)
        self.assertIn(self.tag, item["resume"])
        json.dumps(snap, ensure_ascii=False)

    def test_hook_context_gives_the_tag_and_unfinished_chats_of_the_same_project_only(self):
        newer = "88888888-2222-3333-4444-555555555555"
        text = self.reg.hook_context("claude-code", newer, cwd=r"C:\Users\x\proj\elsewhere", now=NOW)
        self.assertIn(chats.candidate_tag("claude-code", newer), text)
        self.assertNotIn("UNFINISHED", text, "the cut-off chat belongs to another project")
        # a brand new chat opened in the same project as the cut-off one is offered it
        fresh = self.reg.hook_context("claude-code", newer, cwd=r"C:\Users\x\proj\demo", now=NOW)
        self.assertIn("UNFINISHED", fresh)
        self.assertIn(self.tag, fresh)
        self.assertNotIn("UNFINISHED", self.reg.hook_context("claude-code", "88888888-2222-3333-4444-555555555555", cwd=r"C:\Users\x\proj\demo", fresh=False, now=NOW))


class OtherSources(Base):
    def test_chat_on_steroids_new_format_and_legacy_notes(self):
        d = self.appdata / "chat-on-steroids" / "sessions" / "cf6e2584-3a1e-46c2-ac20-c414dfc15b5c"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": d.name, "title": "گفتگوی آزمایشی", "conversationId": None, "updatedAt": 1793800000000}), encoding="utf-8")
        ev = lambda seq, kind, text: {"kind": kind, "messageId": f"m-{seq}-aaaa", "seq": seq, "time": 1793800000000 + seq, "message": {"text": text}}  # noqa: E731
        msgs = {f"user_message\0m-1-aaaa": ev(1, "user_message", "سلام"), f"assistant_message\0m-2-aaaa": ev(2, "assistant_message", "سلام، بفرما")}
        (d / "messages.json").write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")
        legacy = self.vault / "40-Conversation-Capture" / "f5d88ce3-0e9f-49b8-8621-5d427d3ea0b4"
        legacy.mkdir(parents=True)
        (legacy / "message-m1.md").write_text("---\ntitle: User message\nconversation: x\nupdated: 2026-10-23T11:33:24Z\nsource: cos-session-input\nrole: user\n---\n\n# User message\n\nپیام قدیمی\n", encoding="utf-8")
        self.scan()
        live = self.chat("cos", d.name)
        self.assertEqual((live["title"], live["user_turns"]), ("گفتگوی آزمایشی", 1))
        old = self.chat("cos", legacy.name)
        self.assertEqual((old["user_turns"], old["last_user"]), (1, "پیام قدیمی"))
        self.assertEqual(old["state"], "idle", "only the person's side was saved: no answer is a gap in the log, not a cut-off chat")

    def test_closed_chat_from_a_closing_block(self):
        d = self.vault / "20-Checkpoints" / "session-hz-claude-ai-de62cf892222"
        d.mkdir(parents=True)
        (d / "20261104T163942Z-de62cf892222.md").write_text(
            "---\ntitle: Closing block from claude-ai\nconversation: hz-de62cf892222\nupdated: 2026-11-04T16:39:42Z\nsource: hamzaban-close:claude-ai\n---\n\n"
            "# حافظه جامع بین ایجنت‌ها\n\n## Summary\nساخته شد\n", encoding="utf-8")
        self.scan()
        c = self.chat("claude-ai", "hz-claude-ai-de62cf892222")
        self.assertEqual(c["state"], "closed")
        self.assertTrue(c["tag"].startswith("CA-"))
        self.assertEqual([t["title"] for t in c["topic_list"]], ["حافظه جامع بین ایجنت‌ها"])

    def test_a_broken_store_does_not_hide_the_others(self):
        self.cc_file([cc_user("درخواست", 5), cc_asst("پاسخ", 4)], ago=4)

        class Broken:
            agent = "codex"

            def discover(self, horizon):
                raise RuntimeError("boom")
        reg = chats.Registry(self.vault, sources=[Broken()] + chat_sources.all_sources(self.vault))
        stats = reg.scan(now=NOW)
        self.assertEqual(stats["chats"], 1)
        self.assertEqual(len(stats["errors"]), 1)


class EngineCopy(Base):
    """The engine copied into the temp vault and run as real processes (hook, MCP server, autosync), like on the machine."""

    def setUp(self):
        super().setUp()
        import subprocess
        self.subprocess = subprocess
        self.eng = self.vault / ".ai-memory"
        for f in SRC.glob("*.py"):
            shutil.copy(f, self.eng / f.name)
        shutil.copytree(SRC / "hamzaban", self.eng / "hamzaban", ignore=shutil.ignore_patterns("__pycache__"))
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", SUITE_HOME=str(self.vault),
                        INFINITE_CTX_VAULT=str(self.vault), SUITE_CLAUDE_SETTINGS=str(self.tmp / "nope.json"),
                        SUITE_CODEX_CONFIG=str(self.tmp / "nope.toml"), SUITE_NOW=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.cut_off = self.cc_cut_off_chat()

    def cc_cut_off_chat(self):
        sid = "abababab-2222-3333-4444-555555555555"
        self.cc_file([cc_user("بررسی ساختار پروژه و فایل‌های پیکربندی دیتابیس برای مهاجرت", 600, sid=sid), cc_asst("بررسی شد", 599),
                      cc_user("ساخت صفحه‌ی فرود با تصاویر بازاریابی و فرم تماس مشتریان و رنگ‌های برند جدید", 590, sid=sid),
                      cc_tool("tx", "Bash", {"command": "npm run build"}, 589)], sid=sid, ago=589)
        return chats.candidate_tag("claude-code", sid)

    def run_py(self, *argv, stdin=None):
        return self.subprocess.run([sys.executable, *argv], cwd=str(self.eng), env=self.env, input=stdin, capture_output=True, timeout=120)


class McpChatTests(EngineCopy):
    def call(self, name, args):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}]
        r = self.run_py(str(self.eng / "infinity_mcp.py"), stdin=("\n".join(json.dumps(m) for m in msgs) + "\n").encode("utf-8"))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        res = [json.loads(x) for x in r.stdout.decode("utf-8").splitlines()][-1]["result"]
        return res["isError"], res["content"][0]["text"]

    def test_list_and_one_chat(self):
        err, text = self.call("memory_chats", {"state": "interrupted"})
        self.assertFalse(err)
        self.assertIn(self.cut_off, text)
        err, text = self.call("memory_chats", {"tag": self.cut_off.lower()})
        self.assertIn(f"{self.cut_off}/T2", text)
        self.assertIn("memory_handoff", text)
        self.assertIn("No chats match", self.call("memory_chats", {"query": "zzzz-nothing"})[1])
        for bad in ({"state": "bogus"}, {"tag": 5}, {"limit": "x"}, {"tag": "CC-00000"}):
            self.assertTrue(self.call("memory_chats", bad)[0], bad)

    def test_handoff_by_tag_and_by_topic_tag(self):
        err, text = self.call("memory_handoff", {"topic": self.cut_off})
        self.assertFalse(err)
        self.assertIn("CHAT RESUME", text)
        self.assertIn("وسط اجرای یک ابزار قطع شد", text)
        err, text = self.call("memory_handoff", {"topic": f"{self.cut_off}/T2", "max_chars": 2600})
        self.assertIn("FOCUS", text)
        self.assertIn("ساخت صفحه", text)
        self.assertTrue(self.call("memory_handoff", {"topic": "CC-00000"})[0])
        self.assertTrue(self.call("memory_handoff", {"topic": f"{self.cut_off}/T9"})[0])
        err, text = self.call("memory_handoff", {"target": "generic"})   # the ordinary pack is untouched
        self.assertFalse(err)
        self.assertNotIn("CHAT RESUME", text)


class HookChatTests(EngineCopy):
    def hook(self, **payload):
        data = dict({"hook_event_name": "SessionStart", "session_id": "cdcdcdcd-2222-3333-4444-555555555555", "source": "startup",
                     "cwd": r"C:\Users\x\proj\demo"}, **payload)
        r = self.run_py(str(self.eng / "claude_hook.py"), stdin=json.dumps(data).encode("utf-8"))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        out = r.stdout.decode("utf-8").strip()
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def test_a_new_chat_is_told_its_tag_and_the_cut_off_chats_of_its_project(self):
        self.scan()
        text = self.hook()
        self.assertIn("[CHAT TAG]", text)
        self.assertIn(chats.candidate_tag("claude-code", "cdcdcdcd-2222-3333-4444-555555555555"), text)
        self.assertIn("UNFINISHED CHATS", text)
        self.assertIn(self.cut_off, text)
        self.assertNotIn("UNFINISHED", self.hook(cwd=r"C:\Users\x\other-project"))

    def test_resumed_and_compacted_sessions_get_the_tag_again_but_no_offers(self):
        self.scan()
        for source in ("resume", "compact"):
            text = self.hook(source=source)
            self.assertIn("[CHAT TAG]", text, source)
            self.assertNotIn("UNFINISHED", text, source)

    def test_the_hook_never_breaks_without_a_registry(self):
        text = self.hook(session_id="efefefef-2222-3333-4444-555555555555")
        self.assertIn("[CHAT TAG]", text)
        self.assertNotIn("UNFINISHED", text)


class InterfaceTests(EngineCopy):
    HOSTILE = '"><script>alert(1)</script><img src=x onerror=alert(2)> </script><!-- <script>'

    def setUp(self):
        super().setUp()
        sid = "cafecafe-2222-3333-4444-555555555555"
        self.cc_file([cc_user("بررسی ساختار پروژه " + self.HOSTILE + " و فایل‌های پیکربندی دیتابیس برای مهاجرت نسخه‌ی جدید", 700, sid=sid),
                      cc_asst("بررسی شد " + self.HOSTILE, 699), cc_user("ادامه بده " + self.HOSTILE, 698, sid=sid)], sid=sid, ago=698)
        self.hostile_tag = chats.candidate_tag("claude-code", sid)
        self.scan()

    def page(self):
        r = self.run_py(str(self.eng / "control_room.py"))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return (self.vault / "00-Dashboard" / "control-room.html").read_text(encoding="utf-8")

    def test_the_page_has_three_tabs_the_chat_its_topics_and_copy_buttons(self):
        html = self.page()
        for needle in ('role="tablist"', 'id="p-chats"', 'id="p-skills"', 'id="p-flow"', f'id="c-{self.hostile_tag}"',
                       f'data-copy="ادامه بده از {self.hostile_tag}"', f'data-copy="{self.hostile_tag}/T1"', 'data-state="interrupted"',
                       "obsidian://open?vault="):
            self.assertIn(needle, html, needle)
        self.assertIn("نیمه‌کاره", html)
        self.assertIn("بی‌پاسخ", html)   # why it counts as cut off

    def test_chat_text_can_never_become_markup_or_script(self):
        html = self.page()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        # exactly the page's own scripts: the app script, and one text/plain resume block per resumable chat/topic
        self.assertEqual(html.count("<script>"), 1)
        for block in re.findall(r'<script type="text/plain"[^>]*>(.*?)</script>', html, re.S):
            self.assertNotIn("<", block, "no '<' inside the plain-text resume blocks")
        self.assertIn("\\u003cscript>alert(1)", html, "the resume text keeps the characters, encoded")

    def test_the_dashboard_note_lists_cut_off_chats(self):
        r = self.run_py(str(self.eng / "dashboard.py"), "--print")
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        note = r.stdout.decode("utf-8")
        self.assertIn("چت‌ها و تگ‌ها", note)
        self.assertIn(self.hostile_tag, note)
        self.assertIn("ادامه بده از <تگ>", note)


class NowBandTests(EngineCopy):
    """The top of the chats tab: which chats are being written to right now, and which were cut off and wait for 'continue'."""

    def test_the_page_says_what_is_active_and_what_was_cut_off_and_when_the_tokens_return(self):
        reset = int(datetime(2036, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp())   # far ahead of the real clock the page uses
        uuid = "0badc0de-1111-2222-3333-444444444444"
        self.cx_file([cx("task_started", {}, 30), cx_user("ادامه بده", 29)] + CutOffTests().limit_end(1, reset), uuid=uuid, ago=1)
        live_sid = "5afe5afe-2222-3333-4444-555555555555"
        self.cc_file([cc_user("بررسی ساختار پروژه و فایل‌های پیکربندی", 5, sid=live_sid), cc_tool("tl", "Bash", {"command": "ls"}, 1)], sid=live_sid, ago=1)
        self.scan()
        r = self.run_py(str(self.eng / "control_room.py"))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        html = (self.vault / "00-Dashboard" / "control-room.html").read_text(encoding="utf-8")
        band = re.search(r'<section class="nowband".*?</section>', html, re.S).group(0)
        cut_tag, live_tag = chats.candidate_tag("codex", uuid), chats.candidate_tag("claude-code", live_sid)
        on_col, cut_col = band.split('<div class="nowcol cut">')
        self.assertIn(live_tag, on_col)
        self.assertIn("همین الان در حال کار است", on_col)
        self.assertNotIn(cut_tag, on_col, "a chat the agent itself stopped is not 'active', however fresh its log is")
        self.assertIn(cut_tag, cut_col)
        self.assertIn("سقف مصرف (توکن) تمام شد", cut_col)
        self.assertIn("برمی‌گردد", cut_col)
        self.assertIn(f'data-copy="ادامه بده از {cut_tag}"', cut_col)
        self.assertIn(f'data-state="interrupted"', html)
        self.assertRegex(html, rf'id="c-{cut_tag}"[^>]*data-state="interrupted"')


class CaptureTagTests(EngineCopy):
    def test_hook_notes_carry_the_chat_tag_for_obsidian(self):
        sid = "beadbead-2222-3333-4444-555555555555"
        transcript = self.cc_file([cc_user("از این به بعد همیشه کوتاه بنویس لطفا", 20, sid=sid), cc_asst("باشه", 19)], sid=sid, ago=19)
        payload = {"hook_event_name": "SessionEnd", "session_id": sid, "transcript_path": str(transcript), "cwd": r"C:\Users\x\proj\demo"}
        r = self.run_py(str(self.eng / "claude_hook.py"), stdin=json.dumps(payload).encode("utf-8"))
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        tag = chats.candidate_tag("claude-code", sid)
        for folder in ("20-Checkpoints", "40-Conversation-Capture"):
            notes = list((self.vault / folder / (f"session-{sid}" if folder == "20-Checkpoints" else sid)).glob("*.md"))
            self.assertTrue(notes, folder)
            head = notes[0].read_text(encoding="utf-8").split("---", 2)[1]
            self.assertIn(f"chat: {tag}", head)
            self.assertIn(f"tags: [chat/{tag}]", head)


class AutosyncChatTests(EngineCopy):
    def test_every_pass_refreshes_the_registry_and_writes_the_notes(self):
        r = self.run_py(str(self.eng / "autosync.py"), "once")
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        status = json.loads((self.eng / "chats-status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["chats"], status["errors"]), (1, []))
        self.assertTrue((self.vault / "60-Chats" / f"{self.cut_off}.md").is_file())
        self.assertEqual(chats.Registry(self.vault).get(self.cut_off)["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()

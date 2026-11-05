"""Memory Suite dashboard: writes a plain-Persian status note into the Obsidian vault.

    python dashboard.py            write/refresh the note now
    python dashboard.py --print    print it instead of writing

The note lives at <Vault>/00-Dashboard/داشبورد حافظه.md (excluded from the search index). autosync.py
refreshes it after each pass, but only when something changed or every 30 minutes, so Obsidian
is not disturbed every minute. Read-only towards everything else: it never changes settings or skills.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import suite

DASH_DIR = suite.VAULT / "00-Dashboard"
DASH_FILE = DASH_DIR / "داشبورد حافظه.md"
STATE = suite.ENGINE / "dashboard-state.json"
REFRESH_SECONDS = 30 * 60
LAST: dict = {}   # what build() collected last; control_room.py draws the visual page from it

ICON = {suite.PASS: "\U0001F7E2 سالم", suite.WARN: "\U0001F7E1 نیاز به توجه",
        suite.FAIL: "\U0001F534 خراب", suite.UNKNOWN: "⚪ نامعلوم"}

# check title (suite.CHECKS) -> plain Persian label
LABELS = {
    "Capture heartbeat": "برنامهٔ ثبت گفتگو (Chat On Steroids) زنده است؟",
    "Last captured message": "\u0622\u062e\u0631\u06cc\u0646 \u06af\u0641\u062a\u06af\u0648\u06cc \u062b\u0628\u062a\u200c\u0634\u062f\u0647 (\u0647\u0645\u0647\u0654 \u0645\u0646\u0627\u0628\u0639)",
    "Hook activity": "ثبت خودکار گفتگوهای Claude Code",
    "Claude Code registration": "اتصال Claude Code به دفتر خاطرات",
    "Codex hook reference": "اتصال Codex به دفتر خاطرات",
    "Skills drift": "\u06cc\u06a9\u0633\u0627\u0646 \u0628\u0648\u062f\u0646 \u0627\u0633\u06a9\u06cc\u0644\u200c\u0647\u0627 \u0628\u06cc\u0646 \u0627\u06cc\u062c\u0646\u062a\u200c\u0647\u0627",
    "Index integrity": "سلامت فهرست جست‌وجو",
    "Queue counts": "صف تأیید",
    "Autosync": "همگام‌سازی خودکار اسکیل‌ها",
}
ROOT_FA = {"src": "منبع مشترک", "agents": "Codex (.agents)", "codex": "Codex (.codex)",
           "claude": "Claude", "cos": "Chat On Steroids"}


def _local(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _fm(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")[:2000]
    except OSError:
        return {}
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
    return out


def cos_last() -> str | None:
    """Newest Chat On Steroids message time, read from a temp COPY of capture-state.sqlite3."""
    import shutil, sqlite3, tempfile
    db = suite.ENGINE / "capture-state.sqlite3"
    if not db.is_file():
        return None
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "c.sqlite3"
        shutil.copy2(db, cp)
        con = sqlite3.connect(str(cp))
        try:
            row = con.execute("select max(saved_at) from seen").fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
    return row[0] if row else None


def connect_results() -> dict:
    """Last CONNECT-ALL run, parsed from logs/connect-all.log: label -> (state, text)."""
    log = suite.ENGINE / "logs" / "connect-all.log"
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    start = max((i for i, l in enumerate(lines) if "=== connect_all" in l), default=None)
    if start is None:
        return {}
    res = {}
    for l in lines[start + 1:]:
        body = l.split(" ", 1)[1] if l[:4].isdigit() and " " in l else l
        for label in ("Claude Code", "Codex", "Claude Desktop", "Chat On Steroids"):
            if body.startswith(label + ":"):
                word = body.split(":", 1)[1].strip().split(" ", 1)[0]
                res[label] = (word, body)
    return res


def protected_skills() -> list[str]:
    st = _read_json(suite.ENGINE / "autosync-state.json") or {}
    out = []
    for name, ent in (st.get("skills") or {}).items():
        roots = ent.get("roots") or {}
        cos, src = roots.get("cos"), roots.get("src")
        if isinstance(cos, dict) and isinstance(src, dict) and not cos.get("autosync") and cos.get("canon") != src.get("canon"):
            out.append(name)
    return sorted(out)


def latest_checkpoints(limit: int = 8) -> list[tuple[str, str, str]]:
    root = suite.VAULT / "20-Checkpoints"
    files = []
    if root.is_dir():
        for p in root.rglob("*.md"):
            try:
                files.append((p.stat().st_mtime, p))
            except OSError:
                pass
    files.sort(reverse=True)
    out, seen = [], set()
    for _, p in files:
        if p.parent in seen:
            continue
        seen.add(p.parent)
        fm = _fm(p)
        conv = fm.get("conversation") or p.parent.name.replace("session-", "")
        if fm.get("project") or fm.get("topic"):
            name = fm.get("project") or fm.get("topic")
        elif str(fm.get("source", "")).startswith("cos"):
            name = "Chat On Steroids \u00b7 " + conv[:8]
        else:
            name = p.parent.name
        link = p.relative_to(suite.VAULT).with_suffix("").as_posix()
        out.append((name, _local(fm.get("updated")), link))
        if len(out) >= limit:
            break
    return out


HZ_FILES = ("claude-code", "codex", "gemini", "chatgpt", "generic")


def hamzaban_section() -> tuple[list[str], list[str]]:
    """(todo lines, section lines) about the cross-agent packs. Reads files only; works without the package."""
    state = _read_json(suite.ENGINE / "hamzaban-state.json") or {}
    last = state.get("last") or {}
    latest = suite.VAULT / "50-Handoff" / "latest"
    todo, lines = [], ["", "## انتقال کار بین ایجنت‌ها (همزبان)", ""]
    if not last:
        lines.append("- هنوز بسته‌ای ساخته نشده. خودکار با اولین همگام‌سازی بعدی ساخته می‌شود.")
        return todo, lines
    for e in (last.get("errors") or [])[:2]:
        todo.append(f"همزبان خطا داد: {e}")
    try:
        when = _local(datetime.fromtimestamp((latest / "pack.json").stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))
    except OSError:
        when = "—"
    lines.append(f"- آخرین بستهٔ آماده: {when}")
    lines.append("- برای ادامهٔ کار در هر ایجنت، فایل همان ایجنت را باز کن (یا به‌عنوان پیام اول بده):")
    for name in HZ_FILES:
        f = latest / f"{name}.md"
        if f.is_file():
            lines.append(f"  - [[50-Handoff/latest/{name}|{name}]]")
    n = last.get("ingested") or 0
    lines.append(f"- بلوک‌های پایان‌چتی که ایجنت‌ها نوشتند و خوانده شد: {n}")
    return todo, lines


def chats_section() -> tuple[list[str], list[str]]:
    """(todo lines, note lines) about chats that were cut off. Reads the chat registry only; silent when there is none."""
    try:
        import chats
        reg = chats.Registry()
        cut = reg.list(("interrupted",), limit=8)
        live = reg.list(("active",), limit=5)
    except Exception:
        return [], []
    if not cut and not live:
        return [], []
    todo = []
    if cut:
        todo.append(f"{len(cut)} چت نیمه‌کاره مانده ({', '.join(r['tag'] for r in cut[:4])}): به هر ایجنتی بگو «ادامه بده از <تگ>».")
    lines = ["", "## چت‌ها و تگ‌ها", "",
             "هر چت یک تگ ثابت دارد و هر موضوعِ داخلش یک زیرتگ (`CC-7K3QF/T3`). نمای کامل با جست‌وجو و دکمهٔ کپی: برگهٔ «چت‌ها و تگ‌ها» در `00-Dashboard/control-room.html`.",
             "", "### نیمه‌کاره‌ها (وسط کار قطع شده)", ""]
    for r in cut:
        why = chats.REASON_FA.get(r["reason"], "")
        lines.append(f"- [[60-Chats/{r['tag']}|{r['tag']}]] · {chats.AGENT_FA.get(r['agent'], r['agent'])} · {why} · {(r['title'] or r['last_user'])[:70]}")
    if not cut:
        lines.append("- چت نیمه‌کاره‌ای نیست.")
    if live:
        lines += ["", "### الان فعال", ""] + [f"- [[60-Chats/{r['tag']}|{r['tag']}]] · {chats.AGENT_FA.get(r['agent'], r['agent'])} · {(r['title'] or r['last_user'])[:70]}" for r in live]
    return todo, lines


def build() -> tuple[str, str]:
    """Return (note_text, content_hash_without_timestamps)."""
    results = []
    for title, fn in suite.CHECKS:
        status, ev = suite.run_check(fn)
        results.append((title, status, ev))
    conn = connect_results()
    prot = protected_skills()
    auto = _read_json(suite.ENGINE / "autosync-status.json") or {}
    q = suite.queue_counts()

    todo = []
    if not conn:
        todo.append("فایل `CONNECT-ALL.cmd` در پوشهٔ `.ai-memory` را یک بار اجرا کن و بعد Claude Desktop را کامل ببند و باز کن.")
    for label, (word, _) in conn.items():
        if word == "FAIL":
            todo.append(f"اتصال {label} خطا داد؛ جزئیات در `logs/connect-all.log`.")
    if prot:
        todo.append(f"{len(prot)} اسکیل در Chat On Steroids با نسخهٔ Codex فرق دارد و دست نخورده مانده؛ تصمیم با توست (پایین صفحه).")
    if q.get("pending"):
        todo.append(f"{q['pending']} یادداشت پیشنهادی در [[30-Pending]] منتظر تأیید توست.")
    for c in auto.get("conflicts") or []:
        todo.append(f"تداخل اسکیل: {c.get('skill') if isinstance(c, dict) else c} (یادداشتش در 30-Pending).")

    hz_todo, hz_lines = hamzaban_section()
    ch_todo, ch_lines = chats_section()
    todo += hz_todo + ch_todo
    L = ["---", "title: داشبورد حافظه", "type: dashboard", "generated-by: dashboard.py", "---", "",
         "# داشبورد حافظه", "",
         "این صفحه خودکار ساخته می‌شود؛ دستی ویرایشش نکن (دفعهٔ بعد رویش نوشته می‌شود).",
         "", "نمای بصری با نمودار جریان: فایل `00-Dashboard/control-room.html` را در مرورگر باز کن (یا `CONTROL-ROOM.cmd`).",
         "", "@@TIME@@", "", "## کارهای منتظر تو", ""]
    L += [f"- [ ] {t}" for t in todo] or ["- هیچ کاری منتظر تو نیست. \U0001F389"]
    L += ["", "## وضعیت هر بخش", "", "| بخش | وضعیت |", "|---|---|"]
    for title, status, _ in results:
        L.append(f"| {LABELS.get(title, title)} | {ICON.get(status, status)} |")
    try:
        last = cos_last()
    except Exception:
        last = None
    if last:
        age_h = (datetime.now(timezone.utc) - datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        mark = ICON[suite.PASS] if age_h < 24 else ICON[suite.WARN]
        L.append(f"| \u0622\u062e\u0631\u06cc\u0646 \u067e\u06cc\u0627\u0645 \u062b\u0628\u062a\u200c\u0634\u062f\u0647\u0654 Chat On Steroids ({_local(last)}) | {mark} |")
    L += ["", "## اتصال ایجنت‌ها (آخرین اجرای CONNECT-ALL)", ""]
    if conn:
        fa = {"ADDED": "\U0001F7E2 وصل شد", "OK": "\U0001F7E2 وصل است", "SKIP": "⚪ پیدا نشد",
              "FAIL": "\U0001F534 خطا", "diagnostic": "\U0001F7E2 گزارش ساخته شد"}
        for label, (word, _) in conn.items():
            L.append(f"- {label}: {fa.get(word, word)}")
    else:
        L.append("- هنوز اجرا نشده.")
    L += ["", "## اسکیل‌ها در هر ایجنت", ""]
    for r in auto.get("roots") or []:
        L.append(f"- {ROOT_FA.get(r.get('name'), r.get('name'))}: {r.get('skills', 0) if r.get('exists') else 'موجود نیست'}")
    notice = suite.ENGINE / "autosync-notice.md"
    moves = []
    try:
        for line in notice.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- (\S+) (install|update) (\S+) from (\S+) to (.+)$", line)
            if m:
                to = ", ".join(ROOT_FA.get(x.strip(), x.strip()) for x in m.group(5).split(","))
                moves.append(f"- {m.group(3)} ← {to}")
    except OSError:
        pass
    if moves:
        L += ["", f"**جابه‌جایی‌های ۲۴ ساعت اخیر** ({len(moves)} مورد، آخرین‌ها):", ""] + moves[-10:]
    L += hz_lines
    L += ch_lines
    L += ["", "## آخرین خلاصهٔ هر کار", ""]
    cps = latest_checkpoints()
    L += [f"- [[{link}|{name}]] — {when}" for name, when, link in cps] or ["- هنوز خلاصه‌ای نیست."]
    if prot:
        L += ["", "## اسکیل‌های متفاوت در Chat On Steroids", "",
              "این‌ها در Chat On Steroids نسخهٔ دیگری دارند و برای احتیاط رویشان نوشته نشد:", "",
              ", ".join(prot)]
    L += ["", "> [!note]- جزئیات فنی (باز کن اگر لازم شد)"]
    for i, (title, status, ev) in enumerate(results, 1):
        L.append(f"> **{i}. {title}: {status}**")
        for e in ev[:6]:
            if e:
                L.append(f"> - {e}")
    text = "\n".join(L) + "\n"
    digest = hashlib.sha256(re.sub(r"\d+[smhd]( \d+[smhd])* ago|\(\d+s ago\)|\d+h \d+m|\d+s old|\d{4}-\d\d-\d\dT[\d:]+Z", "", text).encode("utf-8")).hexdigest()
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    LAST.update(results=results, conn=conn, prot=prot, auto=auto, q=q, todo=todo, cos_last=last, checkpoints=cps)
    return text.replace("@@TIME@@", f"آخرین به‌روزرسانی: {stamp}"), digest


def _write_visual() -> None:
    """Refresh the visual control-room page. Cosmetic: a failure is logged and never touches the note."""
    try:
        import control_room
        control_room.write(DASH_DIR, LAST)
    except Exception as exc:
        try:
            log = suite.ENGINE / "logs" / "control-room.log"
            log.parent.mkdir(exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {type(exc).__name__}: {str(exc)[:200]}\n")
        except OSError:
            pass


def write(force: bool = False) -> bool:
    text, digest = build()
    _write_visual()
    st = _read_json(STATE) or {}
    fresh = (datetime.now(timezone.utc).timestamp() - float(st.get("written_at", 0))) < REFRESH_SECONDS
    if not force and st.get("digest") == digest and fresh and DASH_FILE.is_file():
        return False
    DASH_DIR.mkdir(exist_ok=True)
    tmp = DASH_FILE.with_name(DASH_FILE.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, DASH_FILE)
    STATE.write_text(json.dumps({"digest": digest, "written_at": datetime.now(timezone.utc).timestamp()}), encoding="utf-8")
    return True


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    if "--print" in sys.argv:
        print(build()[0])
    else:
        print("written" if write(force=True) else "unchanged")

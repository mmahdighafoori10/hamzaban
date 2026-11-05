"""Control room: one offline HTML page that shows the memory suite as a picture.

    python control_room.py          write/refresh <Vault>/00-Dashboard/control-room.html and print its path
    CONTROL-ROOM.cmd                same, then open it in the default browser

dashboard.py calls write() with the data it has already collected, so the page costs no extra checks and refreshes
with every autosync pass. The page is a single self-contained file (inline CSS/SVG, no scripts, no network, no
fonts or images fetched), reloads itself every 60 s while open, and only READS the Vault: it never changes
settings, skills or notes. Logs are shown as metadata only (time, event kind, duration); no message text.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import suite

try:
    import app_links
except Exception:  # the page must still be built when that module has a problem: it only adds the "where is it?" panel
    app_links = None

PAGE_NAME = "control-room.html"
REFRESH_SECONDS = 60
OBSIDIAN_VAULT = suite.VAULT.name

OK, WARN, FAIL, UNK, OFF = "ok", "warn", "fail", "unk", "off"
STATE_OF = {suite.PASS: OK, suite.WARN: WARN, suite.FAIL: FAIL, suite.UNKNOWN: UNK}
STATE_FA = {OK: "سالم", WARN: "نیاز به توجه", FAIL: "خراب", UNK: "نامعلوم", OFF: "دستی"}
GLYPH = {OK: "●", WARN: "▲", FAIL: "✕", UNK: "?", OFF: "○"}
EVENT_FA = {"SessionStart": "شروع جلسه", "UserPromptSubmit": "پیام کاربر", "Stop": "پایان پاسخ",
            "PreCompact": "فشرده‌سازی گفتگو", "SessionEnd": "پایان جلسه"}
CHECK_FA = {
    "Capture heartbeat": "برنامهٔ ثبت گفتگو (Chat On Steroids) زنده است؟",
    "Last captured message": "آخرین گفتگوی ثبت‌شده (همهٔ منابع)",
    "Hook activity": "ثبت خودکار گفتگوهای Claude Code",
    "Claude Code registration": "اتصال Claude Code به دفتر خاطرات",
    "Codex hook reference": "اتصال Codex به دفتر خاطرات",
    "Skills drift": "یکسان بودن اسکیل‌ها بین ایجنت‌ها",
    "Index integrity": "سلامت فهرست جست‌وجو",
    "Queue counts": "صف تأیید",
    "Autosync": "همگام‌سازی خودکار اسکیل‌ها",
}
ROOT_FA = {"src": "منبع مشترک", "agents": "Codex (.agents)", "codex": "Codex (.codex)",
           "claude": "Claude", "cos": "Chat On Steroids"}
_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
# The mark of the app (assets/hamzaban.svg drawn small): two voices, and the language they share where they overlap.
MARK = ('<svg class="mark" viewBox="0 0 512 512" aria-hidden="true"><defs><linearGradient id="hzm-bg" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#3b378f"/><stop offset="1" stop-color="#1c1a5e"/></linearGradient>'
        '<clipPath id="hzm-l"><circle cx="192" cy="232" r="128"/></clipPath></defs>'
        '<rect width="512" height="512" rx="120" fill="url(#hzm-bg)"/><path d="M128 343 L84 412 L225 356 Z" fill="#22c7b8"/>'
        '<circle cx="192" cy="232" r="128" fill="#22c7b8"/><path d="M384 343 L428 412 L287 356 Z" fill="#ffb347"/>'
        '<circle cx="320" cy="232" r="128" fill="#ffb347"/><circle cx="320" cy="232" r="128" fill="#fffdf5" clip-path="url(#hzm-l)"/></svg>')
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(MARK.replace('<svg class="mark"', '<svg xmlns="http://www.w3.org/2000/svg"').encode("utf-8")).decode("ascii")


def fa(value) -> str:
    return str(value).translate(_DIGITS)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def _local(ts: str | None) -> str:
    dt = _utc(ts)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def _utc(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ago(ts: str | None) -> str:
    dt = _utc(ts)
    if not dt:
        return "—"
    s = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if s < 90:
        return "همین الان"
    if s < 3600:
        return f"{s // 60} دقیقه پیش"
    if s < 86400:
        return f"{s // 3600} ساعت پیش"
    return f"{s // 86400} روز پیش"


def _clock(ts: str | None) -> str:
    """Local time of a UTC timestamp, in Persian digits: 'HH:MM' for today, 'MM-DD HH:MM' for another day."""
    dt = _utc(ts)
    if not dt:
        return "—"
    try:
        local = dt.astimezone()
    except (OverflowError, OSError, ValueError):
        return "—"
    return fa(local.strftime("%H:%M" if local.date() == datetime.now().astimezone().date() else "%m-%d %H:%M"))


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    """Last `limit` JSON rows of a log; tolerant of a missing file and of a half-written last line."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _bidi(text: str) -> str:
    """Escape; Persian digits for bare numbers; Latin words/paths isolated so '30-Pending' is not flipped in RTL text."""
    out = []
    # one token = a Latin word/path, optionally followed by more Latin words ("Chat On Steroids" stays one unit)
    token = r"[A-Za-z0-9][A-Za-z0-9._/\-]*(?: [A-Za-z][A-Za-z0-9._/\-]*)*"
    for i, piece in enumerate(re.split(f"({token})", text)):
        if i % 2 == 0:
            out.append(esc(piece))
        else:
            out.append(esc(fa(piece)) if piece.isdigit() else f'<bdi dir="ltr">{esc(piece)}</bdi>')
    return "".join(out)


def _plain(todo_line: str) -> str:
    """To-do text from the markdown note -> plain text with `code` kept as <code>."""
    out, parts = "", todo_line.replace("[[", "").replace("]]", "").split("`")
    for i, part in enumerate(parts):
        out += f"<code>{esc(part)}</code>" if i % 2 else _bidi(part)
    return out


def agent_rows(ctx: dict) -> list[dict]:
    """One row per agent: how its work gets INTO the vault (capture) and how memory comes BACK (return)."""
    res = {t: (STATE_OF.get(s, UNK), ev) for t, s, ev in ctx["results"]}
    st = lambda title: res.get(title, (UNK, []))[0]  # noqa: E731
    conn = ctx.get("conn") or {}
    auto = ctx.get("auto") or {}
    cos_roots = {r.get("name"): r for r in (auto.get("roots") or [])}
    prot = ctx.get("prot") or []
    cos_last = ctx.get("cos_last")

    def conn_state(label: str):
        word = (conn.get(label) or ("",))[0]
        return word

    rows = []
    hooks = st("Hook activity")
    rows.append({"name": "Claude Code", "role": "همین ابزار",
                 "cap": (hooks, "ثبت خودکار" if hooks == OK else "ثبت مشکل دارد",
                         "هر پیام و پایان گفتگو خودکار در Vault ذخیره می‌شود." if hooks == OK
                         else "هوک ثبت اخیراً فعالیتی نشان نداده؛ جزئیات در «سلامت بخش‌ها»."),
                 "ret": (st("Claude Code registration"),
                         "حافظه را می‌خواند" if st("Claude Code registration") == OK else "اتصال مشکل دارد",
                         "ابزار infinity-memory و مهارت hamzaban-continue در دسترس است.")})
    codex_cap = st("Codex hook reference")
    cw = conn_state("Codex")
    rows.append({"name": "Codex", "role": "ایجنت کدنویسی",
                 "cap": (codex_cap if codex_cap != OK else OK,
                         "ثبت خودکار" if codex_cap == OK else "ثبت خودکار ندارد",
                         "هوک فعال دارد." if codex_cap == OK
                         else "هوک فعالی برای Codex پیدا نشد؛ فعلاً گفتگوهایش باید دستی به Vault برگردد."),
                 "ret": (OK if cw in ("OK", "ADDED") else (FAIL if cw == "FAIL" else UNK),
                         "حافظه را می‌خواند" if cw in ("OK", "ADDED") else ("اتصال خطا داد" if cw == "FAIL" else "اتصال نامعلوم"),
                         "به MCP حافظه وصل است." if cw in ("OK", "ADDED") else "آخرین اجرای CONNECT-ALL را ببین.")})
    dw = conn_state("Claude Desktop")
    rows.append({"name": "Claude Desktop", "role": "چت",
                 "cap": (OFF, "دستی (بلوک پایان‌چت)", "ثبت خودکار ندارد؛ در پایان چت بلوک hamzaban-close را ایجنت می‌نویسد."),
                 "ret": ((UNK if dw in ("OK", "ADDED") else (FAIL if dw == "FAIL" else UNK)),   # written but not verifiable from here: unknown, not a fault
                         "نوشته شد، تأیید نشده" if dw in ("OK", "ADDED") else ("اتصال خطا داد" if dw == "FAIL" else "اتصال نامعلوم"),
                         "فایل تنظیمات نوشته شده؛ باید در Settings ← Developer ببینی infinity-memory روی running است."
                         if dw in ("OK", "ADDED") else "CONNECT-ALL برای Desktop اجرا نشده یا خطا داده.")})
    hb = st("Capture heartbeat")
    age_h = None
    if cos_last and _utc(cos_last):
        age_h = (datetime.now(timezone.utc) - _utc(cos_last)).total_seconds() / 3600
    alarm = (_read_json(suite.ENGINE / "capture-service-status.json") or {}).get("uncaptured")
    if isinstance(alarm, int) and alarm > 0:
        cos_cap = (FAIL, f"{fa(alarm)} پیام ثبت‌نشده",
                   f"برنامه می‌گوید {alarm} پیام کاربر دارد که ثبت‌کننده نتوانسته بخواند؛ فرمت ذخیره‌سازی برنامه احتمالاً عوض شده.")
    elif hb != OK:
        cos_cap = (hb, "برنامه زنده نیست", "ضربان برنامهٔ ثبت گفتگو دیده نشد.")
    elif age_h is None or age_h > 24:
        cos_cap = (UNK, "زنده، ولی چیزی ثبت نکرده",   # alive, and no new chat for a day: that is not a fault (a broken reader raises the alarm above)
                   f"آخرین پیام ثبت‌شده: {_local(cos_last)}. برنامه زنده است اما گفتگوی تازه‌ای وارد Vault نشده.")
    else:
        cos_cap = (OK, "ثبت می‌شود", f"آخرین پیام ثبت‌شده: {_local(cos_last)}.")
    cos_ok = bool((cos_roots.get("cos") or {}).get("exists"))
    cos_n = (cos_roots.get("cos") or {}).get("skills")
    cos_max = 128  # the app switches its whole Skills library off above this (autosync.ROOT_CAPS)
    if cos_ok and isinstance(cos_n, int) and cos_n > cos_max:
        cos_ret = (FAIL, f"کتابخانهٔ مهارت خاموش ({fa(cos_n)} از {fa(cos_max)})",
                   f"برنامه فقط تا {cos_max} مهارت را می‌پذیرد و بیشتر از آن کل کتابخانه را خاموش می‌کند؛ چند مهارت را از پوشهٔ مهارت‌های برنامه بردار.")
    elif cos_ok and isinstance(cos_n, int) and cos_n == cos_max:
        cos_ret = (WARN, f"مهارت‌ها روی سقف ({fa(cos_n)} از {fa(cos_max)})",
                   "جای خالی نیست؛ همگام‌ساز مهارت تازه را به این برنامه نمی‌فرستد تا کتابخانه خاموش نشود.")
    else:
        cos_ret = ((WARN if prot else OK) if cos_ok else UNK,
                   (f"{len(prot)} اسکیل دست‌نخورده" if prot else "اسکیل‌ها همگام") if cos_ok else "پوشه پیدا نشد",
                   "فقط اسکیل‌ها همگام می‌شوند؛ حافظهٔ گفتگو را از راه فایل نمی‌گیرد." if cos_ok else "")
    rows.append({"name": "Chat On Steroids", "role": "برنامهٔ چت", "cap": cos_cap, "ret": cos_ret})
    rows.append({"name": "ChatGPT / Gemini", "role": "ایجنت‌های بیرونی",
                 "cap": (OFF, "دستی (بلوک پایان‌چت)", "بلوک hamzaban-close را از پاسخ آخر کپی و در Vault بگذار."),
                 "ret": (OFF, "دستی (فایل بسته)", "فایل chatgpt.md یا gemini.md را به‌عنوان پیام اول بده.")})
    return rows


def _pill(x: int, y: int, state: str, text: str, tip: str, width: int = 218) -> str:
    return (f'<g class="pill {state}"><title>{esc(tip)}</title>'
            f'<rect x="{x - width // 2}" y="{y - 13}" width="{width}" height="26" rx="13"/>'
            f'<text x="{x}" y="{y + 4}" text-anchor="middle">{GLYPH[state]} {esc(text)}</text></g>')


def diagram(ctx: dict, rows: list[dict]) -> str:
    q = ctx["q"]
    row_h, top = 96, 24
    height = top + row_h * len(rows) + 10
    vx, vw = 18, 190           # vault box (left)
    ax, aw = 548, 194          # agent boxes (right)
    mid = (vx + vw + ax) // 2
    parts = [f'<svg viewBox="0 0 760 {height}" role="img" aria-labelledby="dg-t dg-d" class="dg">',
             '<title id="dg-t">نمودار جریان حافظه بین ایجنت‌ها و Vault</title>',
             '<desc id="dg-d">برای هر ایجنت دو فلش هست: بالایی ثبت کار ایجنت در Vault و پایینی بازگشت حافظه به ایجنت. '
             'متن همین وضعیت‌ها در فهرست زیر نمودار هم آمده است.</desc>',
             '<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
             '<path d="M0 0 L10 5 L0 10 z" class="ahp"/></marker></defs>']
    vh = row_h * len(rows) - 12
    parts.append(f'<g class="vault"><rect x="{vx}" y="{top}" width="{vw}" height="{vh}" rx="16"/>'
                 f'<text x="{vx + vw // 2}" y="{top + 34}" text-anchor="middle" class="vt">Vault</text>'
                 f'<text x="{vx + vw // 2}" y="{top + 56}" text-anchor="middle" class="vs">دفتر خاطرات مشترک</text>')
    stats = [(fa(q.get("cp_files", 0)), "خلاصهٔ ذخیره‌شده"), (fa(q.get("pending", 0)), "منتظر تأیید تو"),
             (fa(q.get("facts", 0)), "حقیقت تأییدشده")]
    for i, (num, label) in enumerate(stats):
        y = top + 118 + i * 74
        parts.append(f'<text x="{vx + vw // 2}" y="{y}" text-anchor="middle" class="vn">{num}</text>'
                     f'<text x="{vx + vw // 2}" y="{y + 20}" text-anchor="middle" class="vs">{esc(label)}</text>')
    parts.append("</g>")
    for i, r in enumerate(rows):
        y = top + i * row_h
        cs, ct, ctip = r["cap"]
        rs, rt, rtip = r["ret"]
        worst = max((cs, rs), key=lambda s: [OK, OFF, UNK, WARN, FAIL].index(s))
        parts.append(f'<g class="agent {worst}"><rect x="{ax}" y="{y}" width="{aw}" height="{row_h - 14}" rx="14"/>'
                     f'<text x="{ax + aw // 2}" y="{y + 34}" text-anchor="middle" class="an">{esc(r["name"])}</text>'
                     f'<text x="{ax + aw // 2}" y="{y + 56}" text-anchor="middle" class="ar">{esc(r["role"])}</text></g>')
        y1, y2 = y + 22, y + row_h - 36
        parts.append(f'<line class="ln {cs}" x1="{ax}" y1="{y1}" x2="{vx + vw + 2}" y2="{y1}" marker-end="url(#ah)"/>')
        parts.append(f'<line class="ln {rs}" x1="{vx + vw + 2}" y1="{y2}" x2="{ax}" y2="{y2}" marker-end="url(#ah)"/>')
        parts.append(_pill(mid, y1, cs, "ثبت: " + ct, ctip, 232))
        parts.append(_pill(mid, y2, rs, "بازگشت: " + rt, rtip, 232))
    parts.append("</svg>")
    return "".join(parts)


def agent_list(rows: list[dict]) -> str:
    out = ['<ul class="al">']
    for r in rows:
        for label, (s, t, tip) in (("ثبت", r["cap"]), ("بازگشت", r["ret"])):
            out.append(f'<li><span class="chip {s}">{GLYPH[s]} {esc(STATE_FA[s])}</span> '
                       f'<b>{esc(r["name"])}</b> · {label}: {esc(t)}<br><small>{esc(tip)}</small></li>')
    out.append("</ul>")
    return "".join(out)


def check_cards(ctx: dict) -> str:
    out = []
    for title, status, ev in ctx["results"]:
        s = STATE_OF.get(status, UNK)
        body = "".join(f"<li>{esc(e)}</li>" for e in ev[:8] if e)
        out.append(f'<details class="card {s}"><summary><span class="dot {s}">{GLYPH[s]}</span>'
                   f'<span class="ct">{esc(CHECK_FA.get(title, title))}</span>'
                   f'<span class="cs">{esc(STATE_FA[s])}</span></summary>'
                   f'<ul class="ev">{body or "<li>—</li>"}</ul></details>')
    return "".join(out)


def timeline(hook_rows: list[dict]) -> tuple[str, str]:
    """(sparkline svg, list html) of recent Claude Code hook events. Metadata only."""
    rows = hook_rows[-24:]
    if not rows:
        return "", '<p class="mute">هنوز رویدادی ثبت نشده.</p>'
    mx = max((float(r.get("ms") or 0) for r in rows), default=1) or 1
    bars = []
    for i, r in enumerate(rows):
        h = max(3, round(34 * float(r.get("ms") or 0) / mx))
        bad = " bad" if r.get("error") else ""
        bars.append(f'<rect class="bar{bad}" x="{i * 14}" y="{38 - h}" width="10" height="{h}" rx="2"/>')
    spark = (f'<svg viewBox="0 0 {len(rows) * 14} 40" class="spark" role="img" aria-label="مدت اجرای هوک در {len(rows)} رویداد اخیر">'
             + "".join(bars) + "</svg>")
    items = []
    for r in reversed(rows[-10:]):
        s = FAIL if r.get("error") else OK
        err = f' — <span class="err">{esc(r["error"])}</span>' if r.get("error") else ""
        items.append(f'<li><span class="dot {s}">{GLYPH[s]}</span> {esc(EVENT_FA.get(r.get("event"), r.get("event") or "؟"))}'
                     f' <small><bdi dir="ltr">{esc(fa(_local(r.get("ts"))))}</bdi> · {esc(fa(r.get("ms") or 0))} میلی‌ثانیه</small>{err}</li>')
    return spark, '<ul class="tl">' + "".join(items) + "</ul>"


def skills_bars(auto: dict) -> str:
    roots = [r for r in (auto.get("roots") or []) if r.get("exists")]
    top = max((r.get("skills", 0) for r in roots), default=1) or 1
    out = []
    for r in roots:
        n = r.get("skills", 0)
        out.append(f'<div class="sb"><span>{esc(ROOT_FA.get(r.get("name"), r.get("name")))}</span>'
                   f'<div class="track"><div class="fill" style="width:{round(100 * n / top)}%"></div></div>'
                   f'<b>{fa(n)}</b></div>')
    return "".join(out) or '<p class="mute">اطلاعاتی نیست.</p>'


def render(ctx: dict) -> str:
    rows = agent_rows(ctx)
    states = [STATE_OF.get(s, UNK) for _, s, _ in ctx["results"]]
    states += [r["cap"][0] for r in rows if r["cap"][0] != OFF] + [r["ret"][0] for r in rows if r["ret"][0] != OFF]
    bad = sum(1 for s in states if s in (WARN, FAIL))
    banner = FAIL if FAIL in states else (WARN if bad else OK)
    head = "همه‌چیز سالم است" if banner == OK else f"{fa(bad)} مورد نیاز به توجه دارد"
    hook_rows = _read_jsonl(suite.ENGINE / "logs" / "hook-events.jsonl", 60)
    mcp_rows = _read_jsonl(suite.ENGINE / "logs" / "mcp-calls.jsonl", 400)
    err_rows = _read_jsonl(suite.ENGINE / "logs" / "errors.log", 400)
    spark, tl = timeline(hook_rows)
    mcp_recent = sum(1 for r in mcp_rows if (_utc(r.get("ts")) and (datetime.now(timezone.utc) - _utc(r.get("ts"))).total_seconds() < 3600))
    todo = ctx.get("todo") or []
    todo_html = ("".join(f"<li>{_plain(t)}</li>" for t in todo) if todo
                 else '<li class="done">هیچ کاری منتظر تو نیست.</li>')
    cps = "".join(
        f'<li><a href="obsidian://open?vault={esc(OBSIDIAN_VAULT)}&amp;file={esc(link)}">{esc(name)}</a> '
        f'<small><bdi dir="ltr">{esc(fa(when))}</bdi></small></li>' for name, when, link in (ctx.get("checkpoints") or [])
    ) or '<li class="mute">هنوز خلاصه‌ای نیست.</li>'
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    flow = FLOW.format(
        diagram=diagram(ctx, rows), agents=agent_list(rows), todo=todo_html, checks=check_cards(ctx),
        spark=spark, timeline=tl, skills=skills_bars(ctx.get("auto") or {}), cps=cps,
        mcp=esc(fa(mcp_recent)), errs=esc(fa(len(err_rows))),
        errnote=("" if not err_rows else
                 f'<p class="warnnote">▲ {esc(fa(len(err_rows)))} خطا در <code>logs/errors.log</code> ثبت شده '
                 f'(آخرین: <bdi dir="ltr">{esc(fa(_local(err_rows[-1].get("ts"))))}</bdi>).</p>'),
    )
    snap = _chat_snapshot()
    cut = snap.get("counts", {}).get("interrupted", 0)
    return PAGE.format(
        css=CSS, app_css=CSS_APP, js=JS, mark=MARK, favicon=FAVICON, banner=banner, glyph=GLYPH[banner], head=esc(head), stamp=esc(fa(stamp)),
        chat_badge=(f'<span class="n" title="چت نیمه‌کاره">{fa(cut)}</span>' if cut else ""),
        chats=chats_html(snap), skills=skills_html(_skills_matrix()), flow=flow)


# ---------------------------------------------------------------------------------------------------- chats and skills tabs
def _chat_snapshot() -> dict:
    try:
        import chats
        return chats.Registry().snapshot()
    except Exception:  # the page must still open when the registry has a problem
        return {"generated": "", "counts": {}, "chats": []}


def _skills_matrix():
    try:
        return suite.skills_matrix()
    except Exception:
        return None


def _hay(*parts: str) -> str:
    """The text a search matches against, normalised exactly like the page's script normalises what is typed."""
    text = " ".join(p for p in parts if p).casefold().replace("‌", " ")
    text = text.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", text).strip()


def _script_text(text: str) -> str:
    """Raw text for a <script type="text/plain"> block: no HTML escaping there, but no '<' either, so nothing in the text can
    end the block or start a comment. The page's script turns \\u003c back into '<' when the text is copied."""
    return text.replace("<", "\\u003c")


def _rel(ts: str) -> str:
    dt = _utc(ts)
    if not dt:
        return "—"
    s = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if s < 90:
        return "همین الان"
    if s < 3600:
        return f"{fa(s // 60)} دقیقه پیش"
    if s < 86400:
        return f"{fa(s // 3600)} ساعت پیش"
    return f"{fa(s // 86400)} روز پیش"


STATE_BADGE = {"interrupted": "▲", "active": "●", "idle": "○", "closed": "✓", "continued": "↪"}


def _topic_li(t: dict) -> str:
    btns = [f'<button class="btn" data-copy="ادامه بده از {esc(t["tag"])}">کپی دستور ادامه</button>']
    if app_links is not None:
        btns.append(f'<button class="btn" data-where="{esc(t["tag"].split("/")[0])}" data-where-topic="{esc(t.get("n", ""))}">کجاست؟</button>')
    if t.get("resume"):
        btns.append(f'<button class="btn" data-copy-id="r-{esc(t["tag"].replace("/", "-"))}">کپی متن ادامهٔ این موضوع</button>'
                    f'<script type="text/plain" id="r-{esc(t["tag"].replace("/", "-"))}">{_script_text(t["resume"])}</script>')
    extra = ""
    if t.get("last_user"):
        extra += f'<small>آخرین درخواست: {esc(t["last_user"])}</small>'
    if t.get("last_assistant"):
        extra += f'<small>آخرین نتیجه: {esc(t["last_assistant"])}</small>'
    return (f'<li><div class="row"><button class="tag sub" data-copy="{esc(t["tag"])}" title="کپی زیرتگ">{esc(t["tag"])}</button>'
            f'<span class="t">{esc(t["title"])}</span><small>{fa(t.get("turns", 0))} پیام</small>'
            f'{"<span class=chip>موضوع جاری</span>" if t.get("current") else ""}</div>{extra}'
            f'<div class="btns">{"".join(btns)}</div></li>')


def cut_line(c: dict) -> str:
    """Why and when an interrupted chat stopped, as one line of HTML: the reason, the clock time, and when the tokens come back."""
    parts = [esc(c["reason_fa"])] if c.get("reason_fa") else []
    if c.get("cut_at"):
        parts.append(f'قطع شد <bdi dir="ltr">{esc(_clock(c["cut_at"]))}</bdi>')
    if c.get("reset_at"):
        back = _utc(c["reset_at"])
        if back and back <= datetime.now(timezone.utc):
            parts.append("سقف دوباره باز شده؛ می‌توانی ادامه بدهی")
        else:
            parts.append(f'توکن <bdi dir="ltr">{esc(_clock(c["reset_at"]))}</bdi> برمی‌گردد')
    return " · ".join(parts)


def _now_item(c: dict, extra: str, button: bool) -> str:
    tag = esc(c["tag"])
    buttons = (f'<button class="btn" data-copy="{esc(c["phrase"])}">کپی دستور ادامه</button>' if button else "")
    if app_links is not None:
        buttons += f'<button class="btn" data-where="{tag}">کجاست؟</button>'
    btn = f'<div class="btns">{buttons}</div>' if buttons else ""
    return (f'<li><div class="row"><button class="tag" data-copy="{tag}" title="کپی تگ چت">{tag}</button>'
            f'<a class="t" href="#c-{tag}" data-goto="c-{tag}">{_bidi(c["title"][:70])}</a><small>{esc(c["agent_fa"])}</small></div>{extra}{btn}</li>')


def now_band(chats_list: list[dict]) -> str:
    """What is happening right now: the chats being written to, and the chats that were cut off and wait for a 'continue'."""
    live = [c for c in chats_list if c["state"] == "active"]
    cut = [c for c in chats_list if c["state"] == "interrupted"]
    cut.sort(key=lambda c: 0 if c["reason"] in ("limit", "error") else 1)   # a stop the agent's own log states comes first
    active_items = []
    for c in live[:6]:
        when = "همین الان در حال کار است" if c.get("live") else f'آخرین نوشتن: {esc(_rel(c["last_ts"]))}'
        mine = f' · آخرین پیام تو: {esc(_rel(c["last_user_ts"]))}' if c.get("last_user_ts") else ""
        active_items.append(_now_item(c, f'<small class="ok">● {when}{mine}</small>', False))
    cut_items = [_now_item(c, f'<small class="why">▲ {cut_line(c)}</small>', True) for c in cut[:5]]
    more_live = (f'<p class="meta">و {fa(len(live) - 6)} چت فعال دیگر</p>' if len(live) > 6 else "")
    more_cut = (f'<p class="meta">و {fa(len(cut) - 5)} چت نیمه‌کاره دیگر — <a href="#" data-showstate="interrupted">همه را نشان بده</a></p>'
                if len(cut) > 5 else "")
    return (
        '<section class="nowband" aria-label="وضعیت الان"><h2>الان</h2><div class="nowgrid">'
        f'<div class="nowcol on"><h3>● فعال — لاگشان همین الان نوشته می‌شود</h3>'
        + (f'<ul>{"".join(active_items)}</ul>{more_live}' if active_items else '<p class="meta">هیچ چتی فعال نیست.</p>')
        + '</div><div class="nowcol cut"><h3>▲ نیمه‌کاره — منتظر «ادامه بده»</h3>'
        + (f'<ul>{"".join(cut_items)}</ul>{more_cut}' if cut_items else '<p class="meta">هیچ چتی نیمه‌کاره نمانده.</p>')
        + '</div></div><p class="meta">چند چت می‌توانند همزمان فعال باشند (مثلاً Claude و Codex هر دو باز و در حال کار). برنامه فقط لاگ خودِ ایجنت‌ها را '
          'می‌خواند، پس نمی‌داند کدام پنجره جلوی چشم توست؛ ولی می‌داند هر چت کِی آخرین بار نوشته شده و کدام را خودِ ایجنت '
          '(با خطای سقف مصرف یا توقف) نیمه‌کاره گذاشته است.</p></section>')


def _span(first: str, last: str) -> str:
    return (f'از <bdi dir="ltr">{esc(_clock(first))}</bdi> تا <bdi dir="ltr">{esc(_clock(last))}</bdi>') if first and last else ""


def _part_li(c: dict, t: dict) -> str:
    """One part (topic) of a chat: where to look for it inside the application."""
    nums = ""
    if t.get("from_n"):
        total = c.get("turns") or 0
        nums = (f'پیام‌های شمارهٔ {fa(t["from_n"])} تا {fa(t["to_n"])}' if t["to_n"] != t["from_n"] else f'پیام شمارهٔ {fa(t["from_n"])}') + \
               (f' از {fa(total)} پیامِ تو' if total else "")
    when = _span(t.get("first_ts", ""), t.get("last_ts", ""))
    phrase = app_links.search_phrase(t.get("first_user", ""))
    return (f'<li id="wt-{esc(c["tag"])}-{esc(t["n"])}"><div class="row"><button class="tag sub" data-copy="{esc(t["tag"])}" title="کپی زیرتگ">{esc(t["tag"])}</button>'
            f'<span class="t">{esc(t["title"])}</span></div>'
            + (f'<small>{nums}{" · " if nums and when else ""}{when}</small>' if nums or when else "")
            + (f'<small>اولین درخواست در این بخش: {esc(t["first_user"][:160])}</small>' if t.get("first_user") else "")
            + (f'<small>آخرین نتیجهٔ این بخش: {esc(t["last_assistant"][:160])}</small>' if t.get("last_assistant") else "")
            + (f'<div class="btns"><button class="btn" data-copy="{esc(phrase)}">کپی عبارت جست‌وجو</button>'
               f'<small>در برنامه Ctrl+F بزن و این را بچسبان: <bdi dir="ltr">«{esc(phrase)}»</bdi></small></div>' if phrase else "")
            + "</li>")


def where_html(c: dict) -> str:
    """The body of the "where is it?" panel of one chat: the application, the exact part, the files, and the buttons that take you there."""
    if app_links is None:
        return ""
    w = app_links.where(c)
    gone = ' <small>دیگر در این مسیر نیست</small>'
    head = f'<div class="wapp"><div><b>{esc(w["app_fa"])}</b>'
    if w["entry"]:
        head += f'<small>ورودی ثبت‌شده در لاگ: <bdi dir="ltr">{esc(w["entry"])}</bdi></small>'
    head += "</div>"
    if w["primary"]:
        head += f'<a class="btn{" primary" if w["primary"]["exact"] else ""}" href="{esc(w["primary"]["href"])}">{esc(w["primary"]["label"])}</a>'
    head += "</div>"
    out = [head, f'<p class="meta">{esc(w["note"])}</p>' if w["note"] else ""]
    rows = [("عنوان چت", f'{_bidi(c["title"])}'), ("زمان", _span(c.get("first_ts", ""), c.get("last_ts", "")) or "—")]
    if c.get("cwd"):
        rows.append(("پوشهٔ کار", f'<bdi dir="ltr">{esc(c["cwd"])}</bdi> <button class="btn" data-copy="{esc(c["cwd"])}">کپی</button>'
                     + (f' <a class="btn" href="{esc(w["folder_href"])}">باز کردن پوشه</a>' if w["folder_href"] else gone if w["folder_missing"] else "")))
    if c.get("path"):
        rows.append(("فایل چت", f'<bdi dir="ltr">{esc(w["file"] or c["path"])}</bdi> <button class="btn" data-copy="{esc(w["file"] or c["path"])}">کپی</button>'
                     + (f' <a class="btn" href="{esc(w["reveal_href"])}">نمایش در Explorer</a>' if w["reveal_href"] else gone if w["file_missing"] else "")
                     + (" <small>فشرده‌شده (zst)</small>" if w["packed"] else "")))
    if c.get("source_id"):
        rows.append(("شناسهٔ چت", f'<bdi dir="ltr">{esc(c["source_id"])}</bdi> <button class="btn" data-copy="{esc(c["source_id"])}">کپی</button>'))
    out.append('<dl class="wl">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>")
    if w["resume"]:
        r = w["resume"]
        out.append(f'<p class="meta">ادامه در ترمینال: <code>{esc(r["cmd"])}</code> <button class="btn" data-copy="{esc(r["cmd"])}">کپی</button>'
                   + (f' <a class="btn" href="{esc(r["href"])}">باز کردن در ترمینال</a>' if r["available"] and r["href"] else "") + "</p>")
    topics = c.get("topics") or []
    out.append("<h3>کدام بخش از چت؟</h3>")
    if topics:
        out.append('<ol class="wparts">' + "".join(_part_li(c, t) for t in topics) + "</ol>")
        out.append('<p class="meta">برنامه‌ها به پیامِ دقیق نمی‌پرند؛ شمارهٔ پیام و عبارت جست‌وجو نشانی‌اند که بتوانی بخش را در خودِ چت پیدا کنی.</p>')
    else:
        out.append('<p class="meta">این چت موضوع‌بندی نشده؛ از زمان‌های بالا و آخرین درخواست در کارت چت استفاده کن.</p>')
    out.append('<p class="meta">دکمه‌های «باز کردن» و «نمایش» با لینکِ <code>hamzaban:</code> کار می‌کنند؛ اگر چیزی باز نشد، یک‌بار '
               '<code>python shortcuts.py install</code> را در پوشهٔ .ai-memory اجرا کن.</p>')
    return "".join(out)


def chat_card(c: dict) -> str:
    topics = c.get("topics") or []
    hay = _hay(c["tag"], c["title"], c["project"], c["agent_fa"], c["last_user"], " ".join(t["title"] for t in topics),
               " ".join(t["tag"] for t in topics))
    dt = _utc(c["last_ts"])
    stale = "1" if (dt and (datetime.now(timezone.utc) - dt).days >= 30) else "0"
    link = f'obsidian://open?vault={esc(OBSIDIAN_VAULT)}&amp;file={esc("60-Chats/" + c["tag"])}'
    resume_id = "r-" + c["tag"]
    parts = [f'<article class="chat s-{c["state"]}" id="c-{esc(c["tag"])}" data-state="{c["state"]}" data-agent="{esc(c["agent"])}" '
             f'data-old="{stale}" data-hay="{esc(hay)}">',
             f'<div class="top"><button class="tag" data-copy="{esc(c["tag"])}" title="کپی تگ چت">{esc(c["tag"])}</button>'
             f'<h3>{_bidi(c["title"])}</h3><span class="badge {c["state"]}">{STATE_BADGE.get(c["state"], "")} '
             f'{"در حال کار" if c["state"] == "active" and c.get("live") else esc(c["state_fa"])}</span></div>',
             f'<p class="meta">{esc(c["agent_fa"])} · پروژه: <bdi dir="ltr">{esc(c["project"] or "—")}</bdi> · {fa(c["turns"])} پیام · '
             f'آخرین فعالیت: {esc(_rel(c["last_ts"]))}'
             + (f' · آخرین پیام تو: {esc(_rel(c["last_user_ts"]))}' if c.get("last_user_ts") else "") + '</p>']
    if c["state"] == "interrupted" and c["reason_fa"]:
        parts.append(f'<p class="why">▲ {cut_line(c)}</p>')
        if c.get("reason") == "error" and c.get("cut_detail"):
            parts.append(f'<p class="meta"><bdi dir="ltr">{esc(c["cut_detail"][:160])}</bdi></p>')
    if c.get("parent"):
        parts.append(f'<p class="meta">ادامهٔ چت <button class="tag sub" data-copy="{esc(c["parent"])}">{esc(c["parent"])}</button></p>')
    if c.get("continued_by"):
        parts.append(f'<p class="meta">ادامه‌یافته در <button class="tag sub" data-copy="{esc(c["continued_by"])}">{esc(c["continued_by"])}</button></p>')
    if c["last_user"]:
        parts.append(f'<p class="last"><b>آخرین درخواست:</b> {esc(c["last_user"][:240])}</p>')
    if c.get("open_tasks"):
        parts.append('<p class="meta"><b>کارهای باز:</b> ' + " · ".join(esc(x) for x in c["open_tasks"][:4]) + "</p>")
    btns = [f'<button class="btn" data-copy="{esc(c["phrase"])}">کپی دستور ادامه</button>']
    if c.get("resume"):
        btns.append(f'<button class="btn" data-copy-id="{esc(resume_id)}">کپی متن کامل ادامه</button>'
                    f'<script type="text/plain" id="{esc(resume_id)}">{_script_text(c["resume"])}</script>')
    if app_links is not None:
        btns.append(f'<button class="btn where" data-where="{esc(c["tag"])}">کجاست؟</button>')
    btns.append(f'<a class="btn" href="{link}">باز کردن در Obsidian</a>')
    parts.append(f'<div class="btns">{"".join(btns)}</div>')
    if app_links is not None:
        parts.append(f'<template id="w-{esc(c["tag"])}">{where_html(c)}</template>')
    if topics:
        parts.append(f'<details class="topics"><summary>{fa(len(topics))} موضوع (هر کدام زیرتگ دارد)</summary><ol>'
                     + "".join(_topic_li(t) for t in topics) + "</ol></details>")
    parts.append("</article>")
    return "".join(parts)


def chats_html(snap: dict) -> str:
    chats_list = [c for c in snap.get("chats", []) if c.get("kind") != "subagent"]
    counts = snap.get("counts", {})
    if not chats_list:
        return ('<section><h2>چت‌ها و تگ‌ها</h2><p class="empty">هنوز چتی ثبت نشده. همگام‌ساز هر دقیقه لاگ ایجنت‌ها را می‌خواند '
                'و برای هر چت یک تگ می‌سازد.</p></section>')
    agents = []
    for c in chats_list:
        if c["agent"] not in [a[0] for a in agents]:
            agents.append((c["agent"], c["agent_fa"]))
    chips = ['<button class="fchip" data-fstate="all" aria-pressed="true">همه<b>' + fa(len(chats_list)) + "</b></button>"]
    for key in ("interrupted", "active", "idle", "closed", "continued"):
        if counts.get(key):
            chips.append(f'<button class="fchip" data-fstate="{key}" aria-pressed="false">{esc(__import__("chats").STATE_FA[key])}<b>{fa(counts[key])}</b></button>')
    agent_chips = ['<button class="fchip" data-fagent="all" aria-pressed="true">همهٔ ایجنت‌ها</button>'] + [
        f'<button class="fchip" data-fagent="{esc(a)}" aria-pressed="false">{esc(fa_name)}</button>' for a, fa_name in agents]
    return (f'{now_band(chats_list)}<section><h2>چت‌ها و تگ‌ها</h2>'
            f'<p class="lead">هر چت یک تگ ثابت دارد (مثل <code>CC-7K3QF</code>) و هر موضوعی که داخلش مطرح شده یک زیرتگ (<code>CC-7K3QF/T3</code>). '
            f'حتی اگر چت وسط کار قطع شود (توکن تمام شود، برنامه بسته شود)، با تگ پیدا و ادامه داده می‌شود.</p>'
            f'<div class="tools"><input id="chat-q" type="search" placeholder="جست‌وجو: تگ، عنوان، پروژه، موضوع…" aria-label="جست‌وجوی چت‌ها">'
            f'<label><input id="chat-old" type="checkbox"> قدیمی‌ها (بیش از ۳۰ روز)</label>'
            f'<span class="mute">نمایش <b id="chat-count">{fa(len(chats_list))}</b> چت</span></div>'
            f'<div class="chips" role="group" aria-label="فیلتر وضعیت">{"".join(chips)}</div>'
            f'<div class="chips" role="group" aria-label="فیلتر ایجنت" style="margin:8px 0 14px">{"".join(agent_chips)}</div>'
            f'<p id="chat-none" class="empty" hidden>چتی با این فیلتر پیدا نشد.</p>'
            + "".join(chat_card(c) for c in chats_list) + "</section>")


SK_CELL = {"SAME": ("ok", "✓", "یکسان"), "IN-SIBLING": ("ok", "✓", "یکسان (از راه پوشهٔ هم‌خانواده)"),
           "FLAT": ("ok", "✓", "معادل: هر زیرمهارت خودش جداگانه نصب است"),
           "IN-UMBRELLA": ("nest", "⊂", "از داخل یک مهارت مادر در دسترس است"),
           "VARIANT": ("var", "≈", "نسخهٔ مخصوص همین ایجنت، عمداً متفاوت"),
           "OVERSIZE": ("var", "≈", "بزرگ‌تر از سقف برنامه؛ نسخهٔ خلاصهٔ خودش را دارد"),
           "DIFFERS": ("warn", "▲", "متفاوت"), "PROTECTED": ("warn", "▲", "متفاوت (دست نخورده مانده)"),
           "MISSING": ("bad", "✕", "کم است"), "INVALID": ("bad", "✕", "نامعتبر"),
           "UNREACHABLE": ("unk", "?", "پوشه در دسترس نیست"), "NOSOURCE": ("unk", "?", "منبع ندارد")}
SK_ORDER = ["SAME", "FLAT", "VARIANT", "OVERSIZE", "IN-UMBRELLA", "IN-SIBLING", "DIFFERS", "PROTECTED", "MISSING", "INVALID", "UNREACHABLE", "NOSOURCE"]
SK_COLUMNS = (("claude", ("claude",), "Claude Code"), ("codex", ("agents", "codex"), "Codex"), ("cos", ("cos",), "Chat On Steroids"))


def skills_html(m: dict | None) -> str:
    if not m or not m.get("rows"):
        return '<section><h2>مهارت‌ها</h2><p class="empty">اطلاعات مهارت‌ها هنوز آماده نیست.</p></section>'
    by = {(r["skill"], r["target"]): r for r in m["rows"]}
    rows_html, bad_total = [], 0
    for name in m["skills"]:
        cells, bad = [], False
        for _key, targets, label in SK_COLUMNS:
            cands = [by[(name, t)] for t in targets if (name, t) in by]
            if not cands:
                cells.append('<td class="c"><span class="cell unk" title="این ایجنت در تنظیمات نیست">—</span></td>')
                continue
            best = min(cands, key=lambda r: SK_ORDER.index(r["state"]) if r["state"] in SK_ORDER else 99)
            cls, glyph, tip = SK_CELL.get(best["state"], ("unk", "?", best["state"]))
            bad = bad or cls in ("warn", "bad")
            note = ""
            if best.get("changed"):
                note = " — " + ", ".join(best["changed"][:2])
            cells.append(f'<td class="c"><span class="cell {cls}" title="{esc(tip + note)}">{glyph} {esc(tip.split(" (")[0].split(":")[0][:22])}</span></td>')
        bad_total += bad
        rows_html.append(f'<tr data-hay="{esc(_hay(name))}" data-bad="{"1" if bad else "0"}"><td><bdi dir="ltr">{esc(name)}</bdi></td>{"".join(cells)}</tr>')
    cap = (m.get("caps") or {}).get("cos")
    cap_html = ""
    if cap:
        pct = min(100, round(100 * cap["count"] / cap["limit"]))
        full = cap["count"] >= cap["limit"]
        cap_html = (f'<div class="cap"><span>کتابخانهٔ Chat On Steroids</span><div class="track"><div class="fill" style="width:{pct}%;'
                    f'background:var({"--warn" if full else "--accent"})"></div></div><b>{fa(cap["count"])} از {fa(cap["limit"])}</b></div>'
                    '<p class="mute" style="margin:-6px 0 12px;font-size:13px">این برنامه بیشتر از ۱۲۸ مهارت را نمی‌پذیرد و با یکی بیشتر کل کتابخانه را خاموش می‌کند؛ '
                    'برای همین مهارت‌ها در آن «از داخل مهارت مادر» (⊂) هم در دسترس‌اند و همگام‌ساز هیچ‌وقت آن را از سقف رد نمی‌کند.</p>')
    summary = " · ".join(f"{esc(SK_CELL.get(k, ('', '', k))[2])}: {fa(v)}" for k, v in sorted(m["counts"].items(), key=lambda kv: -kv[1]))
    return ('<section><h2>مهارت‌های مشترک بین ایجنت‌ها</h2>'
            f'<p class="lead">{fa(len(m["skills"]))} مهارت در منبع مشترک هست و همگام‌ساز هر دقیقه آن را بین ایجنت‌ها یکی نگه می‌دارد. '
            f'{"فعلاً همه‌چیز همگام است." if not bad_total else fa(bad_total) + " مهارت ناهمگون است."}</p>{cap_html}'
            '<div class="tools"><input id="skill-q" type="search" placeholder="جست‌وجوی مهارت…" aria-label="جست‌وجوی مهارت">'
            '<label><input id="skill-bad" type="checkbox"> فقط ناهمگون‌ها</label>'
            '<span class="mute">نمایش <b id="skill-count">' + fa(len(m["skills"])) + '</b> مهارت</span></div>'
            '<div class="skwrap"><table class="skt"><thead><tr><th>مهارت</th>' + "".join(f"<th>{esc(lbl)}</th>" for *_x, lbl in SK_COLUMNS) +
            "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table></div>"
            f'<p class="mute" style="margin-top:10px;font-size:12px">{summary}</p></section>')


def write(dest_dir: Path, ctx: dict) -> Path:
    """Write the page atomically (a browser reloading mid-write sees the old or the new file, never half)."""
    dest_dir.mkdir(exist_ok=True)
    target = dest_dir / PAGE_NAME
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(ctx), encoding="utf-8")
    os.replace(tmp, target)
    return target


CSS = """
:root{--bg:#f6f4ef;--panel:#fff;--ink:#1d2430;--mute:#6a7482;--line:#e3dfd5;
--ok:#177a4c;--okb:#e4f4ea;--warn:#9a5b00;--warnb:#fdf0d6;--fail:#b42a2a;--failb:#fbe4e4;--unk:#5d6877;--unkb:#e9ecf0;
--accent:#3d3a9e}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#14171c;--panel:#1d222a;--ink:#e8ebf0;--mute:#98a2b0;--line:#2c333d;
--ok:#59d08c;--okb:#173a28;--warn:#f0b24a;--warnb:#3b2d10;--fail:#ff7b7b;--failb:#431c1c;--unk:#9aa5b5;--unkb:#262d37;--accent:#a9a7ff}}
*{box-sizing:border-box}
:root{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 "Dana","Vazirmatn","Segoe UI",Tahoma,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:0 0 10px}
.banner{display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:14px;font-weight:600;border:1px solid}
.banner.ok{background:var(--okb);color:var(--ok);border-color:var(--ok)}
.banner.warn{background:var(--warnb);color:var(--warn);border-color:var(--warn)}
.banner.fail{background:var(--failb);color:var(--fail);border-color:var(--fail)}
.stamp{color:var(--mute);font-size:13px}
section{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:16px}
.grid{display:grid;gap:16px;grid-template-columns:1fr 1fr}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
.dg{width:100%;height:auto;display:block}
.dg .vault rect{fill:var(--accent);opacity:.12;stroke:var(--accent);stroke-width:1.5}
.dg .vault .vt{font-size:20px;font-weight:700;fill:var(--accent)}
.dg .vault .vs{font-size:12px;fill:var(--mute)}.dg .vault .vn{font-size:26px;font-weight:700;fill:var(--ink)}
.dg .agent rect{fill:var(--panel);stroke-width:2}
.dg .agent.ok rect{stroke:var(--ok)}.dg .agent.warn rect{stroke:var(--warn)}.dg .agent.fail rect{stroke:var(--fail)}
.dg .agent.unk rect,.dg .agent.off rect{stroke:var(--unk);stroke-dasharray:5 4}
.dg .an{font-size:15px;font-weight:700;fill:var(--ink)}.dg .ar{font-size:12px;fill:var(--mute)}
.ln{stroke-width:2.5;fill:none}.ln.ok{stroke:var(--ok)}.ln.warn{stroke:var(--warn)}.ln.fail{stroke:var(--fail)}
.ln.unk,.ln.off{stroke:var(--unk);stroke-dasharray:6 5}
.ahp{fill:var(--mute)}
.pill rect{stroke-width:1.5}.pill text{font-size:12px;font-weight:600}
.pill.ok rect{fill:var(--okb);stroke:var(--ok)}.pill.ok text{fill:var(--ok)}
.pill.warn rect{fill:var(--warnb);stroke:var(--warn)}.pill.warn text{fill:var(--warn)}
.pill.fail rect{fill:var(--failb);stroke:var(--fail)}.pill.fail text{fill:var(--fail)}
.pill.unk rect,.pill.off rect{fill:var(--unkb);stroke:var(--unk)}.pill.unk text,.pill.off text{fill:var(--unk)}
.legend{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 0;font-size:13px}
.chip{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid}
.chip.ok,.dot.ok{color:var(--ok);background:var(--okb);border-color:var(--ok)}
.chip.warn,.dot.warn{color:var(--warn);background:var(--warnb);border-color:var(--warn)}
.chip.fail,.dot.fail{color:var(--fail);background:var(--failb);border-color:var(--fail)}
.chip.unk,.chip.off,.dot.unk,.dot.off{color:var(--unk);background:var(--unkb);border-color:var(--unk)}
.dot{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;font-size:11px;border:1px solid}
ul{margin:0;padding:0;list-style:none}
.al li{padding:8px 0;border-top:1px solid var(--line)}.al li:first-child{border-top:0}
small,.mute{color:var(--mute)}
.todo li{padding:8px 12px;margin-bottom:8px;border-radius:10px;background:var(--warnb);border:1px solid var(--warn)}
.todo li.done{background:var(--okb);border-color:var(--ok);color:var(--ok)}
code{background:var(--unkb);padding:0 6px;border-radius:6px;font-family:Consolas,monospace;font-size:13px;direction:ltr;unicode-bidi:embed}
.card{border:1px solid var(--line);border-radius:12px;margin-bottom:8px;background:var(--panel)}
.card summary{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer;list-style:none}
.card summary::-webkit-details-marker{display:none}
.card .ct{flex:1}.card .cs{font-size:12px;color:var(--mute)}
.card.warn{border-color:var(--warn)}.card.fail{border-color:var(--fail)}
.ev{padding:4px 14px 12px 14px;font-size:13px;color:var(--mute);direction:ltr;text-align:left;font-family:Consolas,monospace}
.ev li{padding:2px 0;word-break:break-word}
.spark{width:100%;height:46px;direction:ltr}.bar{fill:var(--accent);opacity:.75}.bar.bad{fill:var(--fail);opacity:1}
.tl li{padding:5px 0;border-top:1px solid var(--line)}.tl li:first-child{border-top:0}.err{color:var(--fail);font-size:13px}
.sb{display:grid;grid-template-columns:130px 1fr 36px;gap:10px;align-items:center;margin:8px 0;font-size:14px}
.track{height:10px;background:var(--unkb);border-radius:99px;overflow:hidden}.fill{height:100%;background:var(--accent);border-radius:99px}
.cps li{padding:6px 0;border-top:1px solid var(--line)}.cps li:first-child{border-top:0}
a{color:var(--accent)}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.kpi{flex:1;min-width:120px;padding:10px 12px;border-radius:12px;background:var(--unkb)}
.kpi b{display:block;font-size:22px}.kpi span{font-size:12px;color:var(--mute)}
.warnnote{color:var(--warn);font-size:13px;margin:8px 0 0}
"""

FLOW = """<section>
  <h2>مسیر حافظه: کار هر ایجنت چطور به Vault می‌رسد و چطور برمی‌گردد</h2>
  {diagram}
  <div class="legend">
    <span class="chip ok">● سالم</span><span class="chip warn">▲ نیاز به توجه</span><span class="chip fail">✕ خراب</span>
    <span class="chip unk">? نامعلوم</span><span class="chip off">○ دستی، خودکار نیست</span>
  </div>
  <details style="margin-top:12px"><summary>همین نمودار به‌صورت متن</summary>{agents}</details>
</section>

<section>
  <h2>کارهای منتظر تو</h2>
  <ul class="todo">{todo}</ul>
</section>

<div class="grid">
  <section>
    <h2>سلامت بخش‌ها <small>(برای دیدن شواهد روی هر کدام بزن)</small></h2>
    {checks}
  </section>
  <div>
    <section>
      <h2>فعالیت زندهٔ Claude Code</h2>
      <div class="kpis"><div class="kpi"><b>{mcp}</b><span>فراخوانی حافظه در ساعت اخیر</span></div>
      <div class="kpi"><b>{errs}</b><span>خطای ثبت‌شده در لاگ</span></div></div>
      {spark}
      {timeline}
      {errnote}
    </section>
    <section>
      <h2>تعداد اسکیل در هر ایجنت</h2>
      {skills}
    </section>
    <section>
      <h2>آخرین خلاصهٔ هر کار</h2>
      <ul class="cps">{cps}</ul>
    </section>
  </div>
</div>
"""

CSS_APP = """
[hidden]{display:none!important}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px;border-bottom:1px solid var(--line)}
.tabs [role=tab]{appearance:none;border:0;background:none;color:var(--mute);font:inherit;font-weight:600;padding:10px 16px;border-bottom:3px solid transparent;cursor:pointer;border-radius:10px 10px 0 0}
.tabs [role=tab][aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.tabs [role=tab]:hover{background:var(--unkb)}
.tabs .n{display:inline-block;min-width:22px;padding:0 7px;margin-inline-start:6px;border-radius:99px;font-size:12px;background:var(--warnb);color:var(--warn);border:1px solid var(--warn)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
.tools input[type=search]{flex:1;min-width:200px;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.fchip{appearance:none;border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit;font-size:13px;padding:4px 12px;border-radius:99px;cursor:pointer}
.fchip[aria-pressed=true]{background:var(--accent);color:var(--panel);border-color:var(--accent)}
.fchip b{font-weight:700;margin-inline-start:4px}
.lead{color:var(--mute);margin:0 0 12px;font-size:14px}
.chat{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px;background:var(--panel)}
.chat.s-interrupted{border-color:var(--warn);box-shadow:inset 4px 0 0 var(--warn)}
.chat.s-active{border-color:var(--ok);box-shadow:inset 4px 0 0 var(--ok)}
.chat .top{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.chat h3{margin:0;font-size:15px;flex:1;min-width:180px}
.tag{appearance:none;font-family:Consolas,"Cascadia Mono",monospace;font-size:13px;direction:ltr;border:1px dashed var(--accent);background:var(--unkb);color:var(--accent);padding:1px 8px;border-radius:8px;cursor:copy;font-weight:700}
.tag.sub{font-weight:600;font-size:12px}
.badge{font-size:12px;font-weight:700;padding:1px 10px;border-radius:99px;border:1px solid}
.badge.interrupted{color:var(--warn);background:var(--warnb);border-color:var(--warn)}
.badge.active{color:var(--ok);background:var(--okb);border-color:var(--ok)}
.badge.idle,.badge.continued{color:var(--unk);background:var(--unkb);border-color:var(--unk)}
.badge.closed{color:var(--accent);background:var(--unkb);border-color:var(--accent)}
.chat .meta{margin:4px 0;font-size:13px;color:var(--mute)}
.chat .why{margin:4px 0;font-size:13px;color:var(--warn);font-weight:600}
.chat.flash{outline:3px solid var(--accent);outline-offset:2px}
.nowband{margin-bottom:18px}
.nowgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin:0 0 8px}
.nowcol{border:1px solid var(--line);border-radius:14px;padding:10px 14px;background:var(--panel)}
.nowcol.on{border-color:var(--ok)}
.nowcol.cut{border-color:var(--warn)}
.nowcol h3{margin:0 0 6px;font-size:14px}
.nowcol.on h3{color:var(--ok)}
.nowcol.cut h3{color:var(--warn)}
.nowcol ul{list-style:none;margin:0;padding:0}
.nowcol li{padding:7px 0;border-top:1px solid var(--line);display:grid;gap:3px;min-width:0}
.nowcol li:first-child{border-top:0}
.nowcol .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.nowcol .t{flex:1;min-width:140px;font-weight:600;color:var(--ink);text-decoration:none;overflow-wrap:anywhere}
.nowcol small{color:var(--mute);line-height:1.6}
.nowcol small.ok{color:var(--ok)}
.nowcol small.why{color:var(--warn);font-weight:600}
.nowcol .btns{margin:2px 0 0}
.nowcol .meta{margin:6px 0 0;font-size:13px;color:var(--mute)}
.chat .last{margin:6px 0;font-size:14px}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 2px}
.btn{appearance:none;border:1px solid var(--accent);background:none;color:var(--accent);font:inherit;font-size:13px;padding:3px 12px;border-radius:9px;cursor:pointer;text-decoration:none;display:inline-block}
.btn:hover{background:var(--accent);color:var(--panel)}
.topics{margin-top:10px}
.topics summary{cursor:pointer;color:var(--accent);font-size:14px}
.topics ol{margin:8px 0 0;padding:0;list-style:none}
.topics li{border-top:1px solid var(--line);padding:8px 0;display:grid;gap:3px}
.topics li .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.topics .t{flex:1;min-width:160px;font-weight:600}
.topics small{display:block;line-height:1.6}
.skt{width:100%;border-collapse:collapse;font-size:14px}
.skt th,.skt td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right}
.skt th{position:sticky;top:0;background:var(--panel);font-size:13px;color:var(--mute)}
.skt td.c{text-align:center}
.skt td:first-child{overflow-wrap:anywhere}
.skwrap{overflow-x:auto}
.topics li .row,.topics li small{min-width:0;overflow-wrap:anywhere}
.cell{display:inline-block;min-width:26px;padding:0 8px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid}
.cell.ok{color:var(--ok);background:var(--okb);border-color:var(--ok)}
.cell.nest,.cell.var{color:var(--accent);background:var(--unkb);border-color:var(--accent)}
.cell.warn{color:var(--warn);background:var(--warnb);border-color:var(--warn)}
.cell.bad{color:var(--fail);background:var(--failb);border-color:var(--fail)}
.cell.unk{color:var(--unk);background:var(--unkb);border-color:var(--unk)}
.cap{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;margin:8px 0 14px}
.toast{position:fixed;inset-inline:0;bottom:22px;margin:auto;width:max-content;max-width:90vw;background:var(--ink);color:var(--panel);padding:8px 18px;border-radius:99px;font-size:14px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.on{opacity:.95}
.empty{padding:22px;text-align:center;color:var(--mute)}
@media (max-width:640px){.skt th:nth-child(4),.skt td:nth-child(4){display:none}}
.brand{display:flex;align-items:center;gap:14px}
.brand .mark{width:52px;height:52px;flex:none;border-radius:14px}
.brand h1{font-size:24px;letter-spacing:.2px}
.tagline{color:var(--mute);font-size:13px}
.btn.primary{background:var(--accent);color:var(--panel);font-weight:700}
.btn.primary:hover{filter:brightness(1.1)}
dialog{border:1px solid var(--line);border-radius:16px;padding:0;background:var(--panel);color:var(--ink);width:min(760px,94vw);max-height:90vh;box-shadow:0 18px 60px rgba(0,0,0,.35)}
dialog::backdrop{background:rgba(14,13,48,.55)}
.dlghead{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.dlghead h2{margin:0;flex:1;font-size:16px}
#where-body{padding:10px 16px 16px;overflow:auto;max-height:calc(90vh - 62px)}
#where-body h3{margin:16px 0 4px;font-size:15px}
.wapp{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:12px 14px;border-radius:12px;background:var(--unkb)}
.wapp b{font-size:16px}.wapp small{display:block;color:var(--mute)}
.wl{display:grid;grid-template-columns:max-content 1fr;gap:8px 16px;margin:12px 0;font-size:14px}
.wl dt{color:var(--mute)}.wl dd{margin:0;overflow-wrap:anywhere}
.wparts{list-style:none;margin:6px 0 0;padding:0}
.wparts li{border-top:1px solid var(--line);padding:9px 0;display:grid;gap:3px}
.wparts li.flash{outline:3px solid var(--accent);outline-offset:2px;border-radius:8px}
.wparts .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.wparts .t{flex:1;min-width:160px;font-weight:600}
.wparts small{display:block;line-height:1.7;overflow-wrap:anywhere}
"""

JS = r"""
(function () {
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var store = {
    get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
  };
  var DIG = '۰۱۲۳۴۵۶۷۸۹';
  function fa(n) { return String(n).replace(/[0-9]/g, function (d) { return DIG.charAt(+d); }); }
  function norm(s) {
    return String(s || '').toLowerCase().replace(/‌/g, ' ').replace(/[يى]/g, 'ی').replace(/ك/g, 'ک').replace(/\s+/g, ' ').trim();
  }

  /* ---- tabs */
  var tabs = $$('[role=tab]'), panels = $$('[role=tabpanel]');
  function show(id) {
    if (!$('#p-' + id)) { id = 'chats'; }
    tabs.forEach(function (t) { var on = t.getAttribute('data-tab') === id; t.setAttribute('aria-selected', on ? 'true' : 'false'); t.tabIndex = on ? 0 : -1; });
    panels.forEach(function (p) { p.hidden = p.id !== 'p-' + id; });
    store.set('cr-tab', id);
    try { if (location.hash !== '#' + id) { history.replaceState(null, '', '#' + id); } } catch (e) {}
  }
  tabs.forEach(function (t, i) {
    t.addEventListener('click', function () { show(t.getAttribute('data-tab')); });
    t.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        var j = (i + (e.key === 'ArrowLeft' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[j].focus(); show(tabs[j].getAttribute('data-tab')); e.preventDefault();
      }
    });
  });

  /* ---- chats: search + state + agent filters */
  var q = $('#chat-q'), old = $('#chat-old'), state = store.get('cr-state') || 'all', agent = store.get('cr-agent') || 'all';
  function applyChats() {
    if (!q) { return; }
    var needle = norm(q.value), n = 0;
    $$('.chat').forEach(function (c) {
      var isOld = c.getAttribute('data-old') === '1' && c.getAttribute('data-state') !== 'interrupted';
      var ok = (state === 'all' || c.getAttribute('data-state') === state) &&
               (agent === 'all' || c.getAttribute('data-agent') === agent) &&
               (!needle || c.getAttribute('data-hay').indexOf(needle) >= 0) &&
               (needle || old.checked || !isOld);
      c.hidden = !ok; if (ok) { n++; }
    });
    $('#chat-count').textContent = fa(n);
    $('#chat-none').hidden = n !== 0;
    $$('[data-fstate]').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-fstate') === state ? 'true' : 'false'); });
    $$('[data-fagent]').forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-fagent') === agent ? 'true' : 'false'); });
    store.set('cr-q', q.value); store.set('cr-state', state); store.set('cr-agent', agent); store.set('cr-old', old.checked ? '1' : '0');
  }
  if (q) {
    q.value = store.get('cr-q') || ''; old.checked = store.get('cr-old') === '1';
    q.addEventListener('input', applyChats); old.addEventListener('change', applyChats);
    $$('[data-fstate]').forEach(function (b) { b.addEventListener('click', function () { state = b.getAttribute('data-fstate'); applyChats(); }); });
    $$('[data-fagent]').forEach(function (b) { b.addEventListener('click', function () { agent = b.getAttribute('data-fagent'); applyChats(); }); });
    applyChats();
    /* the "now" band: jump to a chat's card even when a filter hides it; "show all" opens the cut-off filter */
    $$('[data-goto]').forEach(function (a) {
      a.addEventListener('click', function (e) {
        e.preventDefault(); state = 'all'; agent = 'all'; q.value = ''; applyChats();
        var c = document.getElementById(a.getAttribute('data-goto'));
        if (c) { c.scrollIntoView({ block: 'center' }); c.classList.add('flash'); setTimeout(function () { c.classList.remove('flash'); }, 1800); }
      });
    });
    $$('[data-showstate]').forEach(function (a) {
      a.addEventListener('click', function (e) { e.preventDefault(); state = a.getAttribute('data-showstate'); applyChats(); q.scrollIntoView({ block: 'start' }); });
    });
  }

  /* ---- skills: search + only-mismatches */
  var sq = $('#skill-q'), sbad = $('#skill-bad');
  function applySkills() {
    if (!sq) { return; }
    var needle = norm(sq.value), n = 0;
    $$('.skt tbody tr').forEach(function (r) {
      var ok = (!needle || r.getAttribute('data-hay').indexOf(needle) >= 0) && (!sbad.checked || r.getAttribute('data-bad') === '1');
      r.hidden = !ok; if (ok) { n++; }
    });
    $('#skill-count').textContent = fa(n);
    store.set('cr-sq', sq.value); store.set('cr-sbad', sbad.checked ? '1' : '0');
  }
  if (sq) {
    sq.value = store.get('cr-sq') || ''; sbad.checked = store.get('cr-sbad') === '1';
    sq.addEventListener('input', applySkills); sbad.addEventListener('change', applySkills); applySkills();
  }

  /* ---- copy */
  var toastEl = $('#toast'), toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg; toastEl.classList.add('on');
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { toastEl.classList.remove('on'); }, 1600);
  }
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return legacyCopy(text);
  }
  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', '');
      ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select();
      var ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta); ok ? resolve() : reject();
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-copy],[data-copy-id]') : null;
    if (!b) { return; }
    var text = b.getAttribute('data-copy');
    if (text === null) { var src = document.getElementById(b.getAttribute('data-copy-id')); text = src ? src.textContent.replace(/\\u003c/g, '<') : ''; }
    copy(text).then(function () { toast('کپی شد: ' + (text.length > 40 ? text.slice(0, 38) + '…' : text)); },
                    function () { toast('کپی نشد؛ متن را دستی انتخاب کن'); });
  });

  /* ---- "where is it?": the application, the part of the chat, the files */
  var dlg = $('#where-dlg');
  function closeWhere() { if (dlg && dlg.open) { if (dlg.close) { dlg.close(); } else { dlg.removeAttribute('open'); } } }
  function openWhere(tag, topic) {
    var tpl = document.getElementById('w-' + tag);
    if (!dlg || !tpl) { return; }
    $('#where-body').innerHTML = tpl.innerHTML;
    $('#where-title').textContent = 'کجاست؟ ' + tag;
    if (dlg.showModal) { dlg.showModal(); } else { dlg.setAttribute('open', ''); }
    if (topic) {
      var li = document.getElementById('wt-' + tag + '-' + topic);
      if (li) { li.classList.add('flash'); li.scrollIntoView({ block: 'center' }); }
    }
  }
  document.addEventListener('click', function (e) {
    var t = e.target, b = t.closest ? t.closest('[data-where]') : null;
    if (b) { openWhere(b.getAttribute('data-where'), b.getAttribute('data-where-topic')); return; }
    if (dlg && dlg.open && (t === dlg || (t.closest && t.closest('[data-close]')))) { closeWhere(); }
  });

  /* ---- keep the page fresh without losing what the person was doing */
  window.addEventListener('beforeunload', function () { store.set('cr-scroll', String(window.pageYOffset || 0)); });
  show((location.hash || '').slice(1) || store.get('cr-tab') || 'chats');
  var y = parseInt(store.get('cr-scroll') || '0', 10); if (y) { window.scrollTo(0, y); }
  setInterval(function () {
    var a = document.activeElement;
    if (document.hidden || (a && /INPUT|TEXTAREA/.test(a.tagName)) || String(window.getSelection ? window.getSelection() : '') || $('.chat details[open]') || $('dialog[open]')) { return; }
    location.reload();
  }, 60000);
})();
"""

PAGE = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>همزبان · اتاق کنترل</title>
<link rel="icon" type="image/svg+xml" href="{favicon}">
<style>{css}{app_css}</style>
<noscript><style>[role=tabpanel][hidden]{{display:block!important}}</style></noscript>
</head>
<body>
<div class="wrap">
<header>
  <div class="brand">{mark}<div><h1>همزبان</h1><div class="tagline">اتاق کنترل حافظهٔ مشترک عامل‌ها</div><div class="stamp">آخرین به‌روزرسانی: <bdi dir="ltr">{stamp}</bdi> · هر ۶۰ ثانیه خودکار تازه می‌شود · همه‌چیز محلی است</div></div></div>
  <div class="banner {banner}"><span>{glyph}</span><span>{head}</span></div>
</header>
<nav class="tabs" role="tablist" aria-label="بخش‌های اتاق کنترل">
  <button role="tab" data-tab="chats" aria-selected="true">چت‌ها و تگ‌ها{chat_badge}</button>
  <button role="tab" data-tab="skills" aria-selected="false">مهارت‌ها</button>
  <button role="tab" data-tab="flow" aria-selected="false">مسیر حافظه</button>
</nav>
<main>
<div id="p-chats" role="tabpanel">{chats}</div>
<div id="p-skills" role="tabpanel">{skills}</div>
<div id="p-flow" role="tabpanel">{flow}</div>
</main>
</div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<dialog id="where-dlg" aria-labelledby="where-title"><div class="dlghead"><h2 id="where-title">کجاست؟</h2><button class="btn" data-close type="button">بستن ✕</button></div><div id="where-body"></div></dialog>
<script>{js}</script>
</body>
</html>
"""


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    import dashboard

    dashboard.build()
    print(write(dashboard.DASH_DIR, dashboard.LAST))

"""Local long-term project context: archive, checkpoints, search and resume.

All content remains in the user's Obsidian Vault. The SQLite database is only
an incremental search cache and can be rebuilt from Markdown files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

DEFAULT_VAULT = Path(__file__).resolve().parent.parent
EXCLUDE = {".ai-memory", ".obsidian", ".git", "node_modules", "30-Pending", "00-Dashboard", "50-Handoff"}
CHAR_MAP = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و", "ئ": "ی"})
STOPWORDS = {"در", "از", "به", "که", "رو", "را", "و", "با", "برای", "است", "بود", "شد", "این", "آن", "یک", "the", "a", "is", "and", "of"}


def normalized(text: str) -> str:
    text = text.translate(CHAR_MAP).replace("\u200c", " ")
    text = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).casefold().strip()


def terms(text: str) -> list[str]:
    result = re.findall(r"[^\W_]+", normalized(text), re.UNICODE)
    return list(dict.fromkeys(t for t in result if t not in STOPWORDS)) or result


def slug(text: str) -> str:
    value = re.sub(r"[^\w-]+", "-", normalized(text), flags=re.UNICODE).strip("-_")
    if not value or value.startswith("."):
        raise ValueError("Project name must contain letters or numbers")
    return value[:65]


def markdown_files(vault: Path):
    for file in vault.rglob("*.md"):
        if not set(file.relative_to(vault).parts).intersection(EXCLUDE):
            yield file


def db_open(vault: Path) -> sqlite3.Connection:
    cache = vault / ".ai-memory"
    cache.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(cache / "context-index.sqlite3"), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, title TEXT, content TEXT, normalized TEXT)")
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(path UNINDEXED, title, content, tokenize='unicode61')")
    except sqlite3.OperationalError:
        pass  # Search falls back to scanning the local docs table.
    return conn


def sync(vault: Path, conn: sqlite3.Connection) -> tuple[int, int]:
    # Two hooks can fire on one event and sync the same index at once. Take the write lock BEFORE reading what is
    # indexed: the loser waits, then sees the winner's rows instead of INSERTing the same path (UNIQUE docs.path).
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    previous ={row[0]: (row[1], row[2]) for row in conn.execute("SELECT path, mtime_ns, size FROM docs")}
    present = set()
    changed = 0
    has_fts = bool(conn.execute("SELECT name FROM sqlite_master WHERE name='docs_fts'").fetchone())
    for file in markdown_files(vault):
        rel = file.relative_to(vault).as_posix()
        present.add(rel)
        stat = file.stat()
        if previous.get(rel) == (stat.st_mtime_ns, stat.st_size):
            continue
        try:
            body = file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if re.search(r"^status:\s*(superseded|rejected)\s*$", body, re.MULTILINE):
            stale = conn.execute("SELECT rowid FROM docs WHERE path=?", (rel,)).fetchone()
            if stale:
                if has_fts:
                    conn.execute("DELETE FROM docs_fts WHERE rowid=?", (stale[0],))
                conn.execute("DELETE FROM docs WHERE path=?", (rel,))
            continue
        header = re.search(r"^title:\s*(.+)$", body, re.MULTILINE)
        title = (header.group(1).strip().strip('"') if header else file.stem)
        alias = re.search(r"^aliases:\s*(.+)$", body, re.MULTILINE)
        if alias:
            title += " " + alias.group(1)
        normalized_body = normalized(body)
        old = conn.execute("SELECT rowid FROM docs WHERE path=?", (rel,)).fetchone()
        if old:
            rowid = old[0]
            if has_fts:
                conn.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
            conn.execute("UPDATE docs SET mtime_ns=?,size=?,title=?,content=?,normalized=? WHERE rowid=?",
                         (stat.st_mtime_ns, stat.st_size, title, body, normalized_body, rowid))
        else:
            cur = conn.execute("INSERT INTO docs VALUES (?,?,?,?,?,?)",
                               (rel, stat.st_mtime_ns, stat.st_size, title, body, normalized_body))
            rowid = cur.lastrowid
        if has_fts:
            conn.execute("INSERT INTO docs_fts(rowid,path,title,content) VALUES (?,?,?,?)",
                         (rowid, rel, normalized(title), normalized_body))
        changed += 1
    for path in previous.keys() - present:
        rowid = conn.execute("SELECT rowid FROM docs WHERE path=?", (path,)).fetchone()[0]
        if has_fts:
            conn.execute("DELETE FROM docs_fts WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM docs WHERE path=?", (path,))
    conn.commit()
    return changed, len(present)


def score(row, keywords: list[str], query: str) -> float:
    title = normalized(row["title"])
    body = row["normalized"]
    hits = sum(1 for t in keywords if t in body or t in title)
    if hits == 0:
        return -1
    rank = hits * 5 + sum(12 for t in keywords if t in title)
    if normalized(query) in body:
        rank += 10
    if row["path"].startswith("20-Checkpoints/"):
        rank += 1
    return rank


def ranked(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    keywords = terms(query)
    if not keywords:
        return []
    has_fts = bool(conn.execute("SELECT name FROM sqlite_master WHERE name='docs_fts'").fetchone())
    rows = []
    if has_fts:
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in keywords)
        try:
            rows = list(conn.execute(
                "SELECT d.* FROM docs d JOIN docs_fts f ON d.rowid=f.rowid "
                "WHERE docs_fts MATCH ? LIMIT 250", (expression,)))
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        rows = list(conn.execute("SELECT * FROM docs"))
    rows.sort(key=lambda row: (-score(row, keywords, query), row["path"]))
    return [row for row in rows if score(row, keywords, query) > 0][:limit]


def excerpt(body: str, query: str, max_len: int = 220) -> str:
    keys = terms(query)
    lines = [line.strip() for line in body.splitlines()
             if line.strip() and not line.lstrip().startswith(("---", "#", "title:", "aliases:", "tags:", "updated:", "source:"))]
    if not lines:
        return ""
    best = max(lines, key=lambda line: sum(t in normalized(line) for t in keys))
    return best[:max_len]


def search(vault: Path, query: str, limit: int) -> None:
    with db_open(vault) as conn:
        sync(vault, conn)
        hits = ranked(conn, query, limit)
    if not hits:
        print("No matches in Obsidian. This does not prove the fact never existed.")
    for row in hits:
        print(f"- {row['path']} | {row['title']}")
        print(f"  {excerpt(row['content'], query)}")


def checkpoint(vault: Path, project: str, summary: str, decisions: list[str],
               blockers: list[str], next_steps: list[str], source: str) -> Path:
    now = datetime.now(timezone.utc)
    directory = vault / "20-Checkpoints" / slug(project)
    directory.mkdir(parents=True, exist_ok=True)
    def bullet(items: list[str]) -> str:
        return "\n".join("- " + item.replace("\r", " ").strip() for item in items) if items else "- ثبت نشده"
    note = (f"---\ntitle: وضعیت {project} — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"project: {project}\nupdated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"source: {source}\ntype: checkpoint\n---\n\n"
            f"# آخرین وضعیت {project}\n\n## خلاصه\n{summary.strip()}\n\n"
            f"## تصمیم‌های تأییدشده\n{bullet(decisions)}\n\n"
            f"## موانع و ابهام‌ها\n{bullet(blockers)}\n\n"
            f"## اقدام‌های بعدی\n{bullet(next_steps)}\n")
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:10]
    path = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{digest}.md"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(note)
    return path


def ingest(vault: Path, project: str, source: Path) -> tuple[Path, bool]:
    if not source.is_file() or source.suffix.lower() not in {".md", ".txt", ".json", ".jsonl"}:
        raise ValueError("Import requires an existing .md/.txt/.json/.jsonl file")
    if source.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Import limit is 20 MB per file")
    content = source.read_text(encoding="utf-8-sig")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    directory = vault / "30-Archives" / slug(project)
    directory.mkdir(parents=True, exist_ok=True)
    previous = next(directory.glob(f"*-{digest}.md"), None)
    if previous:
        return previous, False
    name = re.sub(r"[^\w.-]+", "-", source.stem, flags=re.UNICODE).strip(".-")[:55] or "source"
    target = directory / f"{name}-{digest}.md"
    if target.exists():
        return target, False
    header = (f"---\ntitle: Archive — {source.name}\nproject: {project}\n"
              f"source: imported-file ({source.name})\nupdated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
              f"type: raw-archive\nsha256-prefix: {digest}\n---\n\n")
    # Preserve original verbatim. Non-Markdown imports are fenced safely.
    if source.suffix.lower() != ".md":
        longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
        fence = "`" * max(3, longest + 1)
        content = f"{fence}text\n{content}\n{fence}\n"
    with target.open("x", encoding="utf-8") as output:
        output.write(header + content)
    return target, True


def resume(vault: Path, project: str, budget: int, limit: int,
           conversation: str | None = None) -> None:
    if budget < 400:
        raise ValueError("Context budget must be at least 400 characters")
    if conversation and not re.fullmatch(r"[a-zA-Z0-9_-]{6,90}", conversation):
        raise ValueError("Unsafe conversation identifier")
    with db_open(vault) as conn:
        sync(vault, conn)
        conn.row_factory = sqlite3.Row
        folder = "20-Checkpoints/" + slug(project) + "/%"
        checkpoints = list(conn.execute("SELECT * FROM docs WHERE path LIKE ? ORDER BY path DESC LIMIT ?", (folder, max(1, limit))))
        related = ranked(conn, project, max(6, limit + 3))
        session_notes = []
        if conversation:
            session_notes = list(conn.execute(
                "SELECT * FROM docs WHERE path LIKE ? OR path LIKE ?",
                ("20-Checkpoints/session-" + conversation + "/%",
                 "40-Conversation-Capture/" + conversation + "/%")))
            def created_at(row):
                match = re.search(r"^updated:\s*(.+)$", row["content"], re.MULTILINE)
                return match.group(1) if match else ""
            session_notes.sort(key=created_at, reverse=True)
    found = set()
    chosen = []
    # Latest same-conversation evidence precedes general project notes.
    for row in session_notes[:max(2, limit // 2)] + checkpoints + related:
        if row["path"] not in found and not row["path"].startswith(("00-Context/START-HERE", "Welcome")):
            chosen.append(row)
            found.add(row["path"])
        if len(chosen) >= limit:
            break
    heading = (f"# Resume: {project}\n"
               + (f"Conversation: {conversation}\n" if conversation else "")
               + "This is retrieved memory, not guaranteed current ground truth. Verify facts against source files.\n")
    remaining = budget - len(heading)
    parts = [heading]
    for row in chosen:
        label = f"\n## Source: {row['path']}\n"
        if remaining <= len(label) + 80:
            break
        available = min(remaining - len(label) - 30, 2200)
        note = row["content"]
        excerpt_text = note[:available]
        if len(note) > available:
            excerpt_text += "\n[truncated; open source file for details]"
        parts.append(label + excerpt_text + "\n")
        remaining -= len(label + excerpt_text + "\n")
    if not chosen:
        parts.append("\nNo source notes for this project. Do not invent a previous state.")
    print("".join(parts)[:budget])


def main():
    parser = argparse.ArgumentParser(description="Durable project context for Obsidian")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index")
    sub.add_parser("status")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=6)
    p = sub.add_parser("resume")
    p.add_argument("project")
    p.add_argument("--conversation", help="CoS conversation ID; prioritizes newest capture notes from that chat")
    p.add_argument("--budget", type=int, default=7000)
    p.add_argument("--limit", type=int, default=6)
    p = sub.add_parser("checkpoint")
    p.add_argument("--project", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--decision", action="append", default=[])
    p.add_argument("--blocker", action="append", default=[])
    p.add_argument("--next", dest="next_steps", action="append", default=[])
    p.add_argument("--source", default="current-chat-summary")
    p = sub.add_parser("ingest")
    p.add_argument("--project", required=True)
    p.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    vault = args.vault.resolve()
    if not vault.is_dir():
        raise ValueError("Obsidian vault directory does not exist")
    if args.command == "index":
        with db_open(vault) as conn:
            changed, total = sync(vault, conn)
        print(f"Indexed {total} notes ({changed} refreshed).")
    elif args.command == "status":
        with db_open(vault) as conn:
            changed, total = sync(vault, conn)
        print(f"Vault: {vault}\nNotes: {total}\nRefreshed: {changed}\nCheckpoints: {sum(1 for p in markdown_files(vault) if '20-Checkpoints' in p.parts)}")
    elif args.command == "search":
        search(vault, args.query, max(1, min(args.limit, 50)))
    elif args.command == "resume":
        resume(vault, args.project, args.budget, max(1, min(args.limit, 20)), args.conversation)
    elif args.command == "checkpoint":
        path = checkpoint(vault, args.project, args.summary, args.decision, args.blocker, args.next_steps, args.source)
        print(f"Saved: {path.relative_to(vault).as_posix()}")
    elif args.command == "ingest":
        path, created = ingest(vault, args.project, args.file)
        print(f"{'Imported' if created else 'Already imported'}: {path.relative_to(vault).as_posix()}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

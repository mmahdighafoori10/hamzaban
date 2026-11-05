"""Small, dependency-free search tool for an Obsidian Markdown vault.

Run on the connected Windows host. No network, index server, or plugin required.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
IGNORED = {".obsidian", ".ai-memory", ".git", "node_modules"}
PERSIAN_CHARS = str.maketrans({"ي": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و", "ئ": "ی"})
STOPWORDS = {"چی", "چیه", "در", "از", "به", "که", "و", "با", "رو", "را", "برای", "این", "اون", "آن", "یک", "ما", "بود", "شد", "میشه", "پروژه", "آخرین", "قبلی"}


def normalize(value: str) -> str:
    value = value.translate(PERSIAN_CHARS).replace("\u200c", " ")
    value = "".join(c for c in unicodedata.normalize("NFKC", value) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).casefold().strip()


def tokens(value: str) -> list[str]:
    parts = re.findall(r"[^\W_]+", normalize(value), re.UNICODE)
    meaningful = [part for part in parts if part not in STOPWORDS]
    return meaningful or parts


def notes():
    for path in VAULT.rglob("*.md"):
        if not IGNORED.intersection(path.relative_to(VAULT).parts):
            yield path


def snippet(body: str, terms: list[str]) -> str:
    cleaned = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", body, flags=re.DOTALL)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    ranked = sorted(lines, key=lambda line: sum(t in normalize(line) for t in terms), reverse=True)
    return ranked[0][:180] if ranked else ""


def search(query: str, limit: int) -> None:
    query_terms = list(dict.fromkeys(tokens(query)))
    hits = []
    for path in notes():
        try:
            body = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        title_match = re.search(r"^title:\s*(.+)$", body, re.MULTILINE)
        aliases_match = re.search(r"^aliases:\s*(.+)$", body, re.MULTILINE)
        title = normalize(path.stem.replace("-", " ") + " " + (title_match.group(1) if title_match else ""))
        aliases = normalize(aliases_match.group(1) if aliases_match else "")
        content = normalize(body)
        matched = [t for t in query_terms if t in title or t in aliases or t in content]
        if not matched:
            continue
        score = sum(14 if t in title else (10 if t in aliases else 2) for t in matched)
        score += 5 * len(matched) / max(len(query_terms), 1)
        if len(matched) == len(query_terms):
            score += 6
        # Promote contiguous phrases that occur in the note.
        if normalize(query) in content:
            score += 8
        hits.append((score, path, body))
    hits.sort(key=lambda hit: (-hit[0], hit[1].as_posix()))
    for i, (score, path, body) in enumerate(hits[:limit], 1):
        print(f"{i}. {path.relative_to(VAULT).as_posix()} [score={score:.1f}]")
        print(f"   {snippet(body, query_terms)}")
    if not hits:
        print("No matches found in the local Obsidian vault.")


def read(relative: str) -> None:
    path = (VAULT / relative).resolve()
    if not path.is_relative_to(VAULT) or not path.is_file() or path.suffix.lower() != ".md":
        raise ValueError("Only existing Markdown files inside the vault can be read")
    print(path.read_text(encoding="utf-8-sig"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Search/read the user's local Obsidian memory")
    sub = parser.add_subparsers(dest="operation", required=True)
    find = sub.add_parser("search")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=6)
    show = sub.add_parser("read")
    show.add_argument("path")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.operation == "search":
        search(args.query, max(1, args.limit))
    elif args.operation == "read":
        read(args.path)
    else:
        files = list(notes())
        print(f"Vault: {VAULT}")
        print(f"Markdown notes: {len(files)}")
        for path in sorted(files):
            print(path.relative_to(VAULT).as_posix())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

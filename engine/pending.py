"""Staging area for automatically proposed facts. Nothing becomes durable without approval.

  python pending.py list
  python pending.py add "text" [--source S]
  python pending.py approve <id> [--sensitivity unreviewed|public-ok|private]
  python pending.py reject <id>
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or Path(__file__).resolve().parent.parent)
PENDING = VAULT / "30-Pending"
FACTS = VAULT / "10-Facts"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u200c", " ")).strip().casefold()


def fact_id(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:12]


def _exists(ident: str) -> bool:
    return any(p.exists() for p in (PENDING / f"{ident}.md", PENDING / "rejected" / f"{ident}.md", FACTS / f"{ident}.md"))


def stage(text: str, source: str = "auto", session: str = "") -> str | None:
    text = text.strip()
    if not text:
        return None
    ident = fact_id(text)
    if _exists(ident):
        return None
    PENDING.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    note = (f"---\ntitle: Pending fact {ident}\nid: {ident}\nstatus: pending\nsource: {source}\n"
            f"session: {session}\nupdated: {now}\n---\n\n{text}\n")
    with (PENDING / f"{ident}.md").open("x", encoding="utf-8") as handle:
        handle.write(note)
    return ident


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def listing() -> list[tuple[str, str]]:
    rows = []
    if PENDING.is_dir():
        for p in sorted(PENDING.glob("*.md")):
            body = re.sub(r"\A---.*?---\s*", "", _read(p), flags=re.DOTALL).strip().replace("\n", " ")
            rows.append((p.stem, body[:160]))
    return rows


def approve(ident: str, sensitivity: str = "unreviewed") -> Path:
    src = PENDING / f"{ident}.md"
    if not src.is_file():
        raise ValueError(f"No pending fact {ident}")
    body = _read(src)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = body.replace("status: pending", "status: active", 1)
    body = body.replace("\n---\n", f"\napproved: {now}\nsensitivity: {sensitivity}\n---\n", 1)
    FACTS.mkdir(parents=True, exist_ok=True)
    dst = FACTS / f"{ident}.md"
    with dst.open("x", encoding="utf-8") as handle:
        handle.write(body)
    src.unlink()
    return dst


def reject(ident: str) -> Path:
    src = PENDING / f"{ident}.md"
    if not src.is_file():
        raise ValueError(f"No pending fact {ident}")
    rejected = PENDING / "rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    dst = rejected / f"{ident}.md"
    body = _read(src).replace("status: pending", "status: rejected", 1)
    dst.write_text(body, encoding="utf-8")
    src.unlink()
    return dst


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("add"); p.add_argument("text"); p.add_argument("--source", default="manual")
    p = sub.add_parser("approve"); p.add_argument("id"); p.add_argument("--sensitivity", default="unreviewed", choices=["unreviewed", "public-ok", "private"])
    p = sub.add_parser("reject"); p.add_argument("id")
    args = parser.parse_args()
    if args.cmd == "list":
        for ident, text in listing():
            print(f"{ident}  {text}")
    elif args.cmd == "add":
        print(stage(args.text, args.source) or "duplicate or empty")
    elif args.cmd == "approve":
        print("approved ->", approve(args.id, args.sensitivity).relative_to(VAULT).as_posix())
    else:
        print("rejected ->", reject(args.id).relative_to(VAULT).as_posix())


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

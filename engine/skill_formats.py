"""Built-in skill front-matter adapter used by `suite.py skills push` for known targets.

Rules (never invent content):
  * SKILL.md must have a front matter block with non-empty `name` and `description`; otherwise the skill is rejected.
  * Front matter is normalized (BOM removed, LF line endings, trailing spaces trimmed).
  * Keys listed in suite-skills.json `strip_keys` for that target (or "*") are removed; name/description never are.
  * The body (everything after the closing `---`) is kept byte-for-byte. Other files are copied untouched.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

KNOWN_TARGETS = ("agents", "codex", "claude", "cos")
REQUIRED = ("name", "description")
_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_KEY = re.compile(r"([A-Za-z0-9_-]+):(.*)\Z")


# Chat On Steroids appends this section (with two "Preserved ..." paths) to every skill it imports from Codex. It is the
# app's own bookkeeping, not an edit by the user, so it must never make a copy look "different" from the shared source.
_COS_FOOTER = re.compile(r"\n*## Local CoS resource location\r?\n.{0,2500}?Preserved SKILL\.md: `[^`\r\n]*`[ \t]*(?:\r?\n)?", re.DOTALL)


def strip_cos_import_footer(text: str) -> str:
    """Remove the footer block only; anything written after it (for example 'read /skills/x/y/SKILL.md' pointers) stays."""
    m = _COS_FOOTER.search(text)
    if not m:
        return text
    before, after = text[: m.start()].rstrip("\r\n"), text[m.end():].lstrip("\r\n")
    return before + "\n" + ("\n" + after if after else "")


class SkillFormatError(ValueError):
    pass


def _blocks(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    blocks: list[tuple[str | None, list[str]]] = []
    for line in lines:
        m = _KEY.match(line) if line[:1] not in (" ", "\t", "-", "#") else None
        if m:
            blocks.append((m.group(1).lower(), [line]))
        elif blocks:
            blocks[-1][1].append(line)
        else:
            blocks.append((None, [line]))
    return blocks


def _has_value(block_lines: list[str]) -> bool:
    first = _KEY.match(block_lines[0])
    inline = (first.group(2) if first else "").strip()
    if inline and inline not in ("|", ">", "|-", ">-", "|+", ">+"):
        return True
    return any(ln.strip() for ln in block_lines[1:])


def split(data: bytes) -> tuple[list[str], str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SkillFormatError("SKILL.md is not valid UTF-8")
    m = _FM.match(text)
    if not m:
        raise SkillFormatError("SKILL.md has no front matter block; name and description are required")
    return m.group(1).replace("\r\n", "\n").split("\n"), text[m.end():]


def adapt(data: bytes, target: str | None = None, strip_keys: list[str] | None = None) -> bytes:
    fm_lines, body = split(data)
    blocks = _blocks(fm_lines)
    present = {k: blk for k, blk in blocks if k}
    for key in REQUIRED:
        if key not in present or not _has_value(present[key]):
            raise SkillFormatError(f"front matter is missing a non-empty '{key}'")
    if target is None:  # canonical form (used to compare copies): the importer's footer and trailing blank lines are not content
        body = strip_cos_import_footer(body)
        if body.strip():
            body = body.rstrip("\r\n") + "\n"
    drop = {str(k).lower() for k in (strip_keys or []) if isinstance(k, str)} - set(REQUIRED)
    kept: list[str] = []
    for key, blk in blocks:
        if key in drop:
            continue
        kept.extend(ln.rstrip() for ln in blk)
    while kept and not kept[-1].strip():
        kept.pop()
    return ("---\n" + "\n".join(kept) + "\n---\n" + body).encode("utf-8")


def strip_for(strip_cfg, target: str) -> list[str]:
    if not isinstance(strip_cfg, dict):
        return []
    keys: list[str] = []
    for name in ("*", target):
        val = strip_cfg.get(name)
        if isinstance(val, list):
            keys += [k for k in val if isinstance(k, str)]
    return keys


def convert(skill_dir, target_dir, target_name=None, strip_keys=None):
    """Converter-plugin compatible entry: copy skill_dir into target_dir adapting SKILL.md (never deletes)."""
    skill_dir, target_dir = Path(skill_dir), Path(target_dir)
    for src in sorted(skill_dir.rglob("*")):
        if not src.is_file() or src.is_symlink() or "__pycache__" in src.parts:
            continue
        dst = target_dir / src.relative_to(skill_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.relative_to(skill_dir).as_posix() == "SKILL.md":
            dst.write_bytes(adapt(src.read_bytes(), target_name, strip_keys))
        else:
            shutil.copyfile(src, dst)

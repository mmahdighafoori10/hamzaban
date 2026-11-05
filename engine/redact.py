"""Extended secret redaction used before anything is written to the Vault."""
from __future__ import annotations

import re

from auto_capture import scrub as _base_scrub


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){15,19}(?![\w-])")


def _card(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[REDACTED-CARD]" if 15 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


_LONG = re.compile(r"(?<![\w/.\\-])[A-Za-z0-9+/_-]{40,}={0,2}(?![\w/-])")


def _long(match: re.Match) -> str:
    token = match.group(0)
    if re.search(r"[A-Za-z]", token) and re.search(r"\d", token) and not re.fullmatch(r"[0-9a-fA-F]{40}", token):
        return "[REDACTED-TOKEN]"
    return token


EXTRA = (
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED-JWT]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b"), "[REDACTED-HEX-KEY]"),
    (re.compile(r"\bsuiprivkey1[0-9a-z]{20,}\b"), "[REDACTED-SUI-KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED-SLACK-TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "[REDACTED-GOOGLE-KEY]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:PRIVATE_KEY|SECRET_KEY)|MNEMONIC|SEED_PHRASE)\b(\s*[:=]\s*)(\S+)"), r"\1\2[REDACTED]"),
)


def scrub_all(text: str) -> str:
    text = _base_scrub(text)
    for regex, replacement in EXTRA:
        text = regex.sub(replacement, text)
    text = _CARD.sub(_card, text)
    return _LONG.sub(_long, text)

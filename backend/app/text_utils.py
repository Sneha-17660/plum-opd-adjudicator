"""Small, dependency-free text helpers used across agents and the engine."""
from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Iterable

_TITLES = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "kumari", "master", "baby", "sri"}


def norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def has_phrase(text: Any, phrase: str) -> bool:
    """Whole-word phrase match (tolerates a trailing plural 's')."""
    t, p = norm(text), norm(phrase)
    if not t or not p:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(p)}s?(?![a-z0-9])", t) is not None


def first_match(text: Any, phrases: Iterable[str]) -> str | None:
    for p in phrases:
        if has_phrase(text, p):
            return p
    return None


def to_money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    cleaned = re.sub(r"(?i)(rs\.?|inr|₹|/-)", "", str(value)).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return round(float(m.group(0)), 2) if m else None


def parse_date(value: Any) -> date | None:
    """Parse ISO or Indian (day-first) dates. Returns None when ambiguous/unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _name_tokens(name: Any) -> list[str]:
    return [t for t in norm(name).split() if t not in _TITLES]


def names_match(a: Any, b: Any) -> bool:
    """Tolerant person-name comparison ('minor variations acceptable').

    Accepts case/punctuation/title differences, small typos, an extra middle or
    family name, and initials ("R. Kumar" ~ "Rajesh Kumar").
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.88:
        return True
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= 2 and set(short) <= set(long_):
        return True
    if len(short) >= 2 and short[-1] == long_[-1]:
        firsts = [t for t in short[:-1]]
        return all(len(t) == 1 and any(x.startswith(t) for x in long_[:-1]) for t in firsts)
    return False


DOCTOR_REG_PATTERN = re.compile(r"^(?:[A-Z]{2,5}/)?[A-Z]{2,5}/\d{2,8}/(\d{4})$")


def doctor_registration_valid(value: Any, on_or_before: date | None = None) -> bool:
    """Format: [State Code]/[Number]/[Year]; AYUSH boards prefix e.g. AYUR/KL/2345/2019."""
    s = re.sub(r"\s+", "", str(value or "").upper())
    m = DOCTOR_REG_PATTERN.match(s)
    if not m:
        return False
    year = int(m.group(1))
    latest = (on_or_before or date.today()).year
    return 1950 <= year <= latest

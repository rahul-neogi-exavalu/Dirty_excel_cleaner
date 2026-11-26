"""Words in a sheet or file name that state a period: months, quarters, halves, weeks,
fiscal years, dates.

Shared by table naming (a sheet named after a period gets the generic ``data`` table),
period detection (month sheets) and the file name's own period, so all three read a name
the same way.

A name is split into words at separators, capitals and digits ("JanJun2025" -> Jan, Jun,
2025; "1H25" -> 1, H, 25). A full month name always states a month. A three-letter one
("Sep", "Dec", "May") is a month only when it is the whole name or stands next to a
number or another month -- "SEP Plans" and "Dec Adj" are about something else, "Sep 2025",
"Jun-25" and "JanJun" are periods. A bare year counts only near the file's own years when
those are known, so "Plan 2000" in a 2026 file is a name, not a date.
"""

from __future__ import annotations

import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_FULL = {"january", "february", "march", "april", "june", "july", "august", "september", "sept",
         "october", "november", "december"}
_WORD = re.compile(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?![a-z])|\d+")
# Dates written with digits: 2024-07, 202407, 07-2024, 07/31/2024.
_NUMERIC = re.compile(
    r"(?<!\d)(?:19|20)\d{2}[-_/. ]?(?:0?[1-9]|1[0-2])(?!\d)"
    r"|(?<!\d)(?:0?[1-9]|1[0-2])[-_/. ](?:19|20)\d{2}(?!\d)"
    r"|(?<!\d)\d{1,2}[-_/.]\d{1,2}[-_/.]\d{2,4}(?!\d)"
)


def words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text or "")]


def _month(word: str) -> int | None:
    if word in _FULL or word in MONTHS:
        return MONTHS[word[:3]]
    return None


def months_in(text: str) -> list[int]:
    """The months a name states, in the order written (see the module notes)."""
    found = []
    tokens = words(text)
    for index, word in enumerate(tokens):
        month = _month(word)
        if month is None:
            continue
        if word in _FULL and word != "may":
            found.append(month)
            continue
        neighbours = tokens[max(index - 1, 0):index] + tokens[index + 1:index + 2]
        alone = all(_month(other) is not None for other in tokens)
        if alone or any(other.isdigit() or _month(other) is not None for other in neighbours):
            found.append(month)
    return found


def years_in(text: str) -> list[int]:
    return [int(word) for word in words(text) if len(word) == 4 and word[:2] in ("19", "20")]


def has_period(text: str, years: set[int] | None = None) -> bool:
    """Whether a name carries a month, date, quarter, half, week, fiscal year or year.

    ``years``: the file's own years; a bare year then counts only within one of them.
    """
    if not text:
        return False
    if _NUMERIC.search(text) or months_in(text):
        return True
    tokens = words(text)
    for index, word in enumerate(tokens):
        after = tokens[index + 1] if index + 1 < len(tokens) else ""
        before = tokens[index - 1] if index else ""
        if word in ("fy", "ytd", "mtd", "qtd"):
            return True
        if word == "q" and after in ("1", "2", "3", "4"):
            return True
        if word in ("q1", "q2", "q3", "q4", "h1", "h2"):
            return True
        if word == "h" and (after in ("1", "2") or before in ("1", "2")):
            return True
        if word in ("week", "wk") and after.isdigit():
            return True
        if len(word) == 4 and word.isdigit() and word[:2] in ("19", "20"):
            if years is None or any(abs(int(word) - year) <= 1 for year in years):
                return True
    return False


def period_months(text: str) -> tuple[int, int] | None:
    """The first and last month a name states as words: JanJun -> (1, 6), Jul -> (7, 7),
    Q2 -> (4, 6), H1 -> (1, 6). Numeric dates (2026-06) are a file's date, not its
    period, so they are not read here."""
    months = months_in(text)
    if months:
        return months[0], months[-1]
    tokens = words(text)
    for index, word in enumerate(tokens):
        after = tokens[index + 1] if index + 1 < len(tokens) else ""
        before = tokens[index - 1] if index else ""
        quarter = word[1:] if word in ("q1", "q2", "q3", "q4") else after if word == "q" else None
        if quarter in ("1", "2", "3", "4"):
            first = 3 * int(quarter) - 2
            return first, first + 2
        half = (word[1:] if word in ("h1", "h2") else after if word == "h" and after in ("1", "2")
                else before if word == "h" and before in ("1", "2") else None)
        if half in ("1", "2"):
            return (1, 6) if half == "1" else (7, 12)
    return None

"""What a file name says about its data: the profit center and the file date.

Source files are named like ``PC796_2026-06 796 TPI - AJG Data Submission_796 TPI``:

* **pc_id** -- ``PC`` plus the profit-center number, normalised to four digits so it
  matches the DRT column mapping's keys: ``PC796`` -> ``PC0796``. A sub-office suffix
  is kept: ``PC069_01`` -> ``PC0069_01``.
* **file_date** -- the date the file is for: ``2026-06`` -> 2026-06-01. Several shapes are
  understood (``2026-06-15``, ``202606``, ``06-2026``, ``Jun 2026``); a year alone is not
  a date. The profit-center token is removed first, so ``PC2024`` is never read as 2024.

Either can be missing from a name; the Ingest review then asks the reviewer for it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

# "PC796", "pc_0796", "PC-796", "PC 796", "PC069_01". Not preceded by a letter or digit
# ("NPC12" is not a profit center), and the number is not followed by another digit.
_PC = re.compile(r"(?<![a-z0-9])pc[\s_\-]?(\d{1,5})(?:_(\d{2}))?(?!\d)", re.IGNORECASE)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_WORD = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_SEP = r"[\s_\-./]"
# Best first. Each yields (year, month, day or None).
_DATES = [
    # 2026-06-15, 2026_06_15: one separator, used twice ("2026-06 5 ..." is not the 5th)
    (re.compile(r"(?<!\d)((?:19|20)\d{2})([-_./])(0?[1-9]|1[0-2])\2(0?[1-9]|[12]\d|3[01])(?!\d)"), "ymd2"),
    # 20260615
    (re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"), "ymd"),
    # 2026-06, 2026_6
    (re.compile(rf"(?<!\d)((?:19|20)\d{{2}}){_SEP}(0?[1-9]|1[0-2])(?!\d)"), "ym"),
    # 202606
    (re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(?!\d)"), "ym"),
    # 06-2026 (not Q2 2026 or H1 2026)
    (re.compile(rf"(?<![A-Za-z\d])(0?[1-9]|1[0-2]){_SEP}((?:19|20)\d{{2}})(?!\d)"), "my"),
]
# Words of a name: separators split it, and so does a capital ("JanJun" -> Jan, Jun).
_WORDS = re.compile(r"[A-Z][a-z]+|[a-z]+|[A-Z]+(?![a-z])")
_MONTH_NAME = re.compile(rf"(?:{_MONTH_WORD})", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def normalize_pc_id(value) -> str | None:
    """``PC`` + four-digit number, from anything a person or a file might write:
    ``796``, ``PC796``, ``pc0796``, ``0796.0``, ``PC069_01`` -> ``PC0796`` / ``PC0069_01``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in ("null", "none", "n/a"):
        return None
    match = re.fullmatch(r"(?:pc)?[\s_\-]?0*(\d{1,5})(?:\.0+)?(?:_(\d{2}))?", text, re.IGNORECASE)
    if not match:
        return None
    number = match.group(1).zfill(4)
    return f"PC{number}_{match.group(2)}" if match.group(2) else f"PC{number}"


def pc_number(pc_id: str | None) -> str | None:
    """The four-digit number of a pc_id, as the division table writes it: PC0796 -> 0796."""
    normalized = normalize_pc_id(pc_id)
    return normalized[2:] if normalized else None


def pc_id_from_filename(filename: str) -> str | None:
    """The first ``PCnnn`` token in the file name, normalised; None when there is none."""
    match = _PC.search(Path(filename or "").stem)
    if not match:
        return None
    return normalize_pc_id(f"{match.group(1)}_{match.group(2)}" if match.group(2) else match.group(1))


def file_date_from_filename(filename: str) -> date | None:
    """The date a file name states (first day of the month when only a month is given)."""
    stem = _PC.sub(" ", Path(filename or "").stem)
    for pattern, shape in _DATES:
        for match in pattern.finditer(stem):
            parts = match.groups()
            if shape == "ymd2":
                year, month, day = int(parts[0]), int(parts[2]), int(parts[3])
            elif shape == "ymd":
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            elif shape == "ym":
                year, month, day = int(parts[0]), int(parts[1]), 1
            else:
                year, month, day = int(parts[1]), int(parts[0]), 1
            found = _safe_date(year, month, day)
            if found:
                return found
    # "Jun 2026", "JanJun_2026": a month word and a year. With several months (a
    # Jan-Jun file) the latest one is the file's date.
    years = _YEAR.findall(stem)
    months = [_MONTHS[word[:3].casefold()] for word in _WORDS.findall(stem) if _MONTH_NAME.fullmatch(word)]
    if len(set(years)) == 1 and months:
        return _safe_date(int(years[0]), max(months), 1)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def divisions_for(pc_id: str | None, rows) -> list[str]:
    """Distinct divisions the division table gives a profit center, in table order.

    ``rows``: (division, international_office, profit_center) tuples. Usually one
    division; a profit center listed under two divisions returns both, for the reviewer
    to choose.
    """
    # A sub-office (PC0069_01) belongs to its profit center's division (0069).
    number = (pc_number(pc_id) or "").split("_")[0]
    if not number:
        return []
    found: list[str] = []
    for division, _, profit_center in rows:
        if (pc_number(profit_center) or "").split("_")[0] == number and division and str(division).strip() not in found:
            found.append(str(division).strip())
    return found

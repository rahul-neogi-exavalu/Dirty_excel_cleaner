"""What a file name says about its data: the profit center and the file received date.

Source files are named like ``PC0796_2026-06 796 TPI - AJG Data Submission_796 TPI_06302026``:

* **pc_id** -- ``PC`` plus the profit-center number, normalised to four digits so it
  matches the DRT column mapping's keys: ``PC796`` -> ``PC0796``. A sub-office suffix
  is kept: ``PC069_01`` -> ``PC0069_01``.
* **file received date** -- the team writes it as the name's last full date, usually
  ``_MMDDYYYY`` (``_06302026`` -> 2026-06-30), after any date the report itself carries
  (``Request 20260731_08042026`` -> 2026-08-04). Full dates in any order are read
  (``20260630``, ``06302026``, ``30062026``, ``2026-06-30``, ``6.30.2026``); one that reads
  both month-first and day-first (``07062026``) is taken month-first and noted. A name
  with no full date falls back to a month (``2026-06``, ``202606``, ``06-2026``,
  ``Jun 2026`` -> the 1st); a year alone is not a date. The profit-center token is removed
  first, so ``PC2024`` is never read as 2024.
* **period** -- the months a name states in words (``ARR_JanJun_2026``, ``Q2 2026``). Kept
  for reference only: the reporting dates come from the data's AED / PED / TED, never
  from the name (``ahi_bronze.validation``).

Either can be missing from a name; the Validate step then asks the reviewer for it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import period_tokens

# "PC796", "pc_0796", "PC-796", "PC 796", "PC069_01". Not preceded by a letter or digit
# ("NPC12" is not a profit center), and the number is not followed by another digit. A
# two-digit sub-office suffix is not the month of a date ("PC796_06-2026" is PC0796).
_PC = re.compile(r"(?<![a-z0-9])pc[\s_\-]?(\d{1,5})(?:_(\d{2})(?![\d\-/.]))?(?!\d)", re.IGNORECASE)

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


def pc_tokens(filename: str) -> list[str]:
    """Every distinct ``PCnnn`` token in the file name, normalised, in the order written."""
    found = []
    for match in _PC.finditer(Path(filename or "").stem):
        pc = normalize_pc_id(f"{match.group(1)}_{match.group(2)}" if match.group(2) else match.group(1))
        if pc and pc not in found:
            found.append(pc)
    return found


def pc_id_from_filename(filename: str) -> str | None:
    """The file's profit center: its one ``PCnnn`` token, normalised. None when the name
    has none, or names two different ones (the reviewer then says which)."""
    tokens = pc_tokens(filename)
    return tokens[0] if len(tokens) == 1 else None


def source_system_for(pc_id: str | None) -> str | None:
    """The source system a profit center's files carry: PC0515 -> pc0515."""
    return pc_id.casefold().replace("_", "") if pc_id else None


def period_from_filename(filename: str) -> tuple[str, str] | None:
    """(first month, last month) as ``YYYY-MM`` when the name states its period in words
    with one year: ``ARR_JanJun_2026``, ``Jan-Jul 2026``, ``Q2_2026``, ``H1 2026``."""
    stem = _PC.sub(" ", Path(filename or "").stem)
    years = set(period_tokens.years_in(stem))
    span = period_tokens.period_months(stem)
    if len(years) != 1 or span is None:
        return None
    year = years.pop()
    first, last = span
    if last < first:
        return None
    return f"{year:04d}-{first:02d}", f"{year:04d}-{last:02d}"


# Full dates. Eight digits alone are read year-first, then month-first, then day-first;
# Y-M-D and M-D-Y with separators need the same separator twice and a four-digit year.
_EIGHT = re.compile(r"(?<!\d)(\d{8})(?!\d)")
_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})([-_./])(0?[1-9]|1[0-2])\2(0?[1-9]|[12]\d|3[01])(?!\d)")
_MDY = re.compile(r"(?<![\d.])(\d{1,2})([-_./])(\d{1,2})\2((?:19|20)\d{2})(?!\d)")


def received_date_from_filename(filename: str) -> tuple[date | None, str | None]:
    """The file received date a name states, and a note on how it was read (or None).

    The last full date in the name wins: the team appends the received date after any
    date the report itself carries. Without a full date, a month counts (its 1st).
    """
    stem = _PC.sub(" ", Path(filename or "").stem)
    found: list[tuple[int, date, str | None]] = []
    for match in _YMD.finditer(stem):
        day = _safe_date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
        if day:
            found.append((match.start(), day, None))
    for match in _EIGHT.finditer(stem):
        read = _eight_digits(match.group(1))
        if read:
            found.append((match.start(), *read))
    for match in _MDY.finditer(stem):
        read = _either_order(match.group(0), int(match.group(4)), int(match.group(1)), int(match.group(3)))
        if read:
            found.append((match.start(), *read))
    if found:
        _, day, note = max(found, key=lambda item: item[0])
        return day, note
    month = _month_from_filename(stem)
    if month:
        return month, f"The file name gives only a month; {month:%d %b %Y} is assumed."
    return None, None


def _eight_digits(text: str) -> tuple[date, str | None] | None:
    """20260713 (year first), else 07132026 (month first), else 13072026 (day first)."""
    year_first = _safe_date(int(text[:4]), int(text[4:6]), int(text[6:])) if text[:2] in ("19", "20") else None
    if year_first:
        return year_first, None
    if text[4:6] not in ("19", "20"):
        return None
    return _either_order(text, int(text[4:]), int(text[:2]), int(text[2:4]))


def _either_order(text: str, year: int, first: int, second: int) -> tuple[date, str | None] | None:
    """Month-first (the business's convention), else day-first; a note when both fit."""
    month_first = _safe_date(year, first, second)
    day_first = _safe_date(year, second, first)
    if month_first and day_first and month_first != day_first:
        return month_first, (f"{text} reads as {month_first:%d %b %Y} (month first) or {day_first:%d %b %Y} "
                             "(day first); month first is used.")
    if month_first:
        return month_first, None
    if day_first:
        return day_first, f"{text} is read day first: {day_first:%d %b %Y}."
    return None


def file_date_from_filename(filename: str) -> date | None:
    """The file received date a name states (see :func:`received_date_from_filename`)."""
    return received_date_from_filename(filename)[0]


def _month_from_filename(stem: str) -> date | None:
    """A date from a name with no full date: a month, its 1st."""
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

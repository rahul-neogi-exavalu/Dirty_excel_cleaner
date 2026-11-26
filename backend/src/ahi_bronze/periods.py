"""Which months a file covers: Jan-Jun, July only, Jan-Jul ...

The scenario document decides append versus replace by period, but files do not state
their period. It is read from the data instead -- the accounting / transaction date
columns first, then month-named sheets -- and always shown to a person to confirm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from . import period_tokens

_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# Date columns whose name says they carry the reporting period, best first.
_PREFERENCE = [
    re.compile(r"account\w*.*date|acct\w*.*date|accounting"),
    re.compile(r"transaction\w*.*date|trans\w*.*date|txn"),
    re.compile(r"period|posting|report\w*.*date|booking"),
    re.compile(r"effective"),
]


@dataclass(frozen=True)
class Period:
    """An inclusive span of months, each ``YYYY-MM``."""

    start: str
    end: str

    def overlaps(self, other: "Period") -> bool:
        return self.start <= other.end and other.start <= self.end

    def covers(self, other: "Period") -> bool:
        return self.start <= other.start and other.end <= self.end

    def label(self) -> str:
        return self.start if self.start == self.end else f"{self.start} to {self.end}"

    @staticmethod
    def parse(start: str | None, end: str | None) -> "Period | None":
        if not start:
            return None
        start, end = _month(start), _month(end or start)
        if start is None or end is None:
            return None
        return Period(min(start, end), max(start, end))


@dataclass
class PeriodGuess:
    period: Period | None
    # Where the guess came from, e.g. "column accounting_effective_date" or "sheet names".
    source: str | None
    candidates: list[dict] = field(default_factory=list)


def _month(text: str | None) -> str | None:
    match = re.fullmatch(r"\s*((?:19|20)\d{2})-(0[1-9]|1[0-2])(?:-\d{2})?\s*", text or "")
    return f"{match.group(1)}-{match.group(2)}" if match else None


def _rank(column: str) -> int:
    name = column.casefold()
    for index, pattern in enumerate(_PREFERENCE):
        if pattern.search(name):
            return index
    return len(_PREFERENCE)


def column_candidates(frame: pl.DataFrame) -> list[dict]:
    """Every date column's month span, preferred columns first."""
    found = []
    for name, dtype in frame.schema.items():
        if not (dtype == pl.Date or isinstance(dtype, pl.Datetime)):
            continue
        values = frame[name].drop_nulls()
        if values.is_empty():
            continue
        low, high = values.min(), values.max()
        found.append({
            "source": f"column {name}",
            "column": name,
            "start": f"{low.year:04d}-{low.month:02d}",
            "end": f"{high.year:04d}-{high.month:02d}",
            "rows": len(values),
            "rank": _rank(name),
        })
    found.sort(key=lambda item: (item["rank"], -item["rows"]))
    return found


def sheet_months(sheet_names: list[str]) -> tuple[list[int], int | None]:
    """Month numbers named by the sheets, and a year if one is written there too."""
    months, years = set(), set()
    for name in sheet_names:
        months.update(period_tokens.months_in(name or ""))
        years.update(int(year) for year in _YEAR.findall(name or ""))
    return sorted(months), (years.pop() if len(years) == 1 else None)


def detect(frame: pl.DataFrame, sheet_names: list[str]) -> PeriodGuess:
    candidates = column_candidates(frame)
    months, year = sheet_months(sheet_names)
    if months:
        year = year or (int(candidates[0]["end"][:4]) if candidates else None)
        candidates.append({
            "source": "sheet names",
            "column": None,
            "start": f"{year:04d}-{months[0]:02d}" if year else None,
            "end": f"{year:04d}-{months[-1]:02d}" if year else None,
            "rows": None,
            "rank": 1.5,  # trusted after accounting dates, before effective dates
            "months": months,
        })
        candidates.sort(key=lambda item: (item["rank"], -(item["rows"] or 0)))
    for candidate in candidates:
        period = Period.parse(candidate["start"], candidate["end"])
        if period:
            return PeriodGuess(period, candidate["source"], candidates)
    return PeriodGuess(None, None, candidates)

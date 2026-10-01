"""Bronze text -> typed Silver values, exactly as the AHI document lists.

* Strings: trimmed; blank becomes NULL.
* Dates: the document's formats, in its order, then five-digit Excel serials. A value no
  format reads becomes NULL rather than failing the load, and is counted.
* Decimals: 1200.50, 1,200.50, $1,200.50 and (250.00); unreadable values become NULL
  and are counted.

The document fixes the date convention (month first), so these are applied as a fixed
list rather than the cleaner's per-column ambiguity vote.
"""

from __future__ import annotations

import polars as pl

from ahi_clean.coerce import clean_numeric, compact_dates, excel_serials

# The AHI Bronze -> Silver document, section 5, in its order. chrono's %m and %d accept
# one or two digits, so M/d/yyyy and M-d-yyyy are covered by the same patterns.
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]
DECIMAL = pl.Decimal(18, 2)


def text(series: pl.Series) -> pl.Series:
    cleaned = series.cast(pl.String).str.strip_chars()
    return pl.Series(series.name, cleaned).replace("", None)


def dates(series: pl.Series) -> tuple[pl.Series, int]:
    """Parsed dates, and how many non-blank values could not be read."""
    raw = text(series)
    parsed = pl.Series(series.name, [None] * len(raw), dtype=pl.Date)
    for fmt in DATE_FORMATS:
        # Timestamps are read whole, then reduced to their date.
        attempt = raw.str.to_datetime(fmt, strict=False).dt.date() if "%H" in fmt else raw.str.to_date(fmt, strict=False)
        parsed = parsed.fill_null(attempt)
    parsed = parsed.fill_null(compact_dates(raw)).fill_null(excel_serials(raw))
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def decimals(series: pl.Series) -> tuple[pl.Series, int]:
    raw = text(series)
    numbers = clean_numeric(raw).cast(pl.Float64, strict=False)
    parsed = numbers.round(2).cast(DECIMAL, strict=False)
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def by_type(series: pl.Series, data_type: str) -> tuple[pl.Series, int]:
    if data_type == "date":
        return dates(series)
    if data_type == "decimal":
        return decimals(series)
    return text(series), 0

"""Bronze text -> typed Silver values, exactly as the AHI document lists.

* Strings: trimmed; blank becomes NULL.
* Dates: the document's formats, in its order, then five-digit Excel serials. A value no
  format reads becomes NULL rather than failing the load, and is counted.
* Decimals: 1200.50, 1,200.50, $1,200.50, (250.00) and 12%; rounded to the column's
  scale (decimal(18,2), decimal(10,6)); unreadable values become NULL and are counted.
  A percent keeps its number: "12%" is 12.
* Integers: whole numbers ("2026", "2026.0"); a month column also reads month names.
* Booleans: true/false, yes/no, y/n, t/f, 1/0.
* Timestamps: the date formats plus ISO date-times.

The document fixes the date convention (month first), so these are applied as a fixed
list rather than the cleaner's per-column ambiguity vote.
"""

from __future__ import annotations

import polars as pl

from ahi_clean.coerce import clean_numeric, compact_dates, excel_serials

# The AHI Bronze -> Silver document, section 5, in its order. chrono's %m and %d accept
# one or two digits, so M/d/yyyy and M-d-yyyy are covered by the same patterns.
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"]
DATETIME_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]
TRUE = {"true", "t", "yes", "y", "1", "1.0"}
FALSE = {"false", "f", "no", "n", "0", "0.0"}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


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


def timestamps(series: pl.Series) -> tuple[pl.Series, int]:
    raw = text(series)
    parsed = pl.Series(series.name, [None] * len(raw), dtype=pl.Datetime("us", "UTC"))
    for fmt in DATETIME_FORMATS:
        parsed = parsed.fill_null(raw.str.to_datetime(fmt, strict=False, time_zone="UTC"))
    day, _ = dates(series)
    parsed = parsed.fill_null(day.cast(pl.Datetime("us")).dt.replace_time_zone("UTC"))
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def decimals(series: pl.Series, precision: int = 18, scale: int = 2) -> tuple[pl.Series, int]:
    raw = text(series)
    numbers = clean_numeric(raw).cast(pl.Float64, strict=False)
    # A value too large for the column is unreadable for it, not an error for the load.
    limit = 10 ** (precision - scale)
    numbers = _where(numbers, numbers.abs() < limit)
    parsed = numbers.round(scale).cast(pl.Decimal(precision, scale), strict=False)
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def integers(series: pl.Series, dtype=pl.Int32, months: bool = False) -> tuple[pl.Series, int]:
    """Whole numbers; with ``months``, also Jan..Dec / January..December."""
    raw = text(series)
    numbers = clean_numeric(raw).cast(pl.Float64, strict=False)
    whole = _where(numbers, numbers == numbers.round(0))
    parsed = whole.cast(dtype, strict=False)
    if months:
        named = raw.str.to_lowercase().str.slice(0, 3).replace_strict(MONTHS, default=None, return_dtype=dtype)
        parsed = parsed.fill_null(named)
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def _where(values: pl.Series, keep: pl.Series) -> pl.Series:
    """``values`` where ``keep`` is true, NULL elsewhere (NULL ``keep`` included)."""
    frame = pl.DataFrame({"v": values, "k": keep})
    return frame.select(pl.when(pl.col("k")).then(pl.col("v")).otherwise(None)).to_series().alias(values.name)


def booleans(series: pl.Series) -> tuple[pl.Series, int]:
    raw = text(series).str.to_lowercase()
    parsed = pl.Series(series.name, [True if v in TRUE else False if v in FALSE else None for v in raw.to_list()],
                       dtype=pl.Boolean)
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed, invalid


def by_type(series: pl.Series, column) -> tuple[pl.Series, int]:
    """``column``: a catalog SilverColumn (or a bare type name, for older callers)."""
    kind = getattr(column, "kind", None) or ("decimal" if str(column).startswith("decimal") else str(column))
    if kind == "text":
        kind = "string"
    if kind == "date":
        return dates(series)
    if kind == "timestamp":
        return timestamps(series)
    if kind == "decimal":
        return decimals(series, getattr(column, "precision", 0) or 18, getattr(column, "scale", 2) if hasattr(column, "scale") else 2)
    if kind == "int":
        return integers(series, pl.Int32, months=str(getattr(column, "name", "")).endswith("_month"))
    if kind == "bigint":
        return integers(series, pl.Int64)
    if kind == "boolean":
        return booleans(series)
    return text(series), 0

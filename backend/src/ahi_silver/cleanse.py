"""Bronze text -> typed Silver values, as the AHI document lists them.

* Strings: trimmed; blank becomes NULL.
* Dates: the document's formats, in its order (yyyy-MM-dd HH:mm:ss, yyyy-MM-dd,
  MM/dd/yyyy, M/d/yyyy, yyyyMMdd, MM-dd-yyyy, M-d-yyyy), then five-digit Excel serials.
  A year is always four digits: '1/5/26' is not read as the year 26. Fractional seconds
  and ISO 'T' date-times are read too. yyyyMMdd and Excel serials count only between
  1900 and 2100, so an amount or an id is never taken for a date. A value no format reads
  becomes NULL rather than failing the load, and is counted.
* Decimals: 1200.50, 1,200.50, $1,200.50, (250.00) and $(250.00) for a negative value,
  1.2E+03, and 12% (a percent sign divides by 100: 0.12). Values are read exactly (no
  binary floating point) and rounded half up to the column's scale (decimal(18,2),
  decimal(10,6)). Anything else -- '1 200,50', 'USD 100', a value too large for the
  column -- is unreadable for the column: NULL, and counted.
* Integers: whole numbers ("2026", "2026.0"); a month column also reads month names.
* Booleans: true/false, yes/no, y/n, t/f, 1/0.
* Timestamps: the date-time formats, then the dates above.

The document fixes the date convention (month first), so these are applied as a fixed
list rather than the cleaner's per-column ambiguity vote.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import polars as pl

from ahi_clean.coerce import compact_dates, excel_serials

# The AHI Bronze -> Silver document, section 5, in its order, plus the same date-times
# with fractional seconds or an ISO 'T'. chrono's %m and %d accept one or two digits, so
# M/d/yyyy and M-d-yyyy are covered by the same patterns; %.f also accepts no fraction.
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y",
                "%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]
DATETIME_FORMATS = ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M",
                    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]
# What a date can look like at all: a four-digit year, or yyyyMMdd, or an Excel serial.
DATE_SHAPE = (r"^(?:\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?"
              r"|\d{1,2}[/-]\d{1,2}[/-]\d{4}(?: \d{1,2}:\d{2}(?::\d{2})?)?|\d{8}|\d{5})$")
# yyyyMMdd and Excel serials outside these years are numbers, not dates.
EARLIEST, LATEST = date(1900, 1, 1), date(2100, 12, 31)
TRUE = {"true", "t", "yes", "y", "1", "1.0"}
FALSE = {"false", "f", "no", "n", "0", "0.0"}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
CURRENCY = "$£€¥₹"
_DIGITS = re.compile(r"(?:\d{1,3}(?:,\d{3})+(?:\.\d*)?|\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def text(series: pl.Series) -> pl.Series:
    cleaned = series.cast(pl.String).str.strip_chars()
    return pl.Series(series.name, cleaned).replace("", None)


def dates(series: pl.Series) -> tuple[pl.Series, int]:
    """Parsed dates, and how many non-blank values could not be read."""
    raw = text(series)
    shaped = pl.DataFrame({"v": raw}).select(
        pl.when(pl.col("v").str.contains(DATE_SHAPE)).then(pl.col("v")).otherwise(None)).to_series()
    parsed = pl.Series(series.name, [None] * len(raw), dtype=pl.Date)
    for fmt in DATE_FORMATS:
        # Date-times are read whole, then reduced to their date.
        attempt = (shaped.str.to_datetime(fmt, strict=False).dt.date() if "%H" in fmt
                   else shaped.str.to_date(fmt, strict=False))
        parsed = parsed.fill_null(attempt)
    parsed = parsed.fill_null(_bounded(compact_dates(shaped))).fill_null(_bounded(excel_serials(shaped)))
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def _bounded(days: pl.Series) -> pl.Series:
    """Dates between EARLIEST and LATEST; NULL otherwise."""
    return _where(days, (days >= EARLIEST) & (days <= LATEST))


def timestamps(series: pl.Series) -> tuple[pl.Series, int]:
    raw = text(series)
    parsed = pl.Series(series.name, [None] * len(raw), dtype=pl.Datetime("us", "UTC"))
    for fmt in DATETIME_FORMATS:
        parsed = parsed.fill_null(raw.str.to_datetime(fmt, strict=False, time_zone="UTC"))
    day, _ = dates(series)
    parsed = parsed.fill_null(day.cast(pl.Datetime("us")).dt.replace_time_zone("UTC"))
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed.alias(series.name), invalid


def parse_number(value: str | None, percent: bool = True) -> Decimal | None:
    """One amount as an exact Decimal, or None when it is not a plain number.

    Accepts one sign, one currency symbol, balanced parentheses (negative), thousands
    commas in groups of three, a decimal point, an exponent and -- with ``percent`` -- a
    percent sign, which divides by 100.
    """
    rest = (value or "").strip()
    if not rest:
        return None
    sign = currency = opened = closed = pct = False
    negative = False
    while rest:
        if rest[0] in "+-" and not sign:
            sign, negative = True, negative or rest[0] == "-"
        elif rest[0] in CURRENCY and not currency:
            currency = True
        elif rest[0] == "(" and not opened:
            opened = True
        else:
            break
        rest = rest[1:].strip()
    while rest:
        if rest[-1] == ")" and opened and not closed:
            closed = True
        elif rest[-1] == "%" and percent and not pct:
            pct = True
        elif rest[-1] in CURRENCY and not currency:
            currency = True
        else:
            break
        rest = rest[:-1].strip()
    if opened != closed or not _DIGITS.fullmatch(rest):
        return None
    try:
        number = Decimal(rest.replace(",", ""))
    except InvalidOperation:
        return None
    if opened:
        negative = True
    if pct:
        number = number / 100
    return -number if negative else number


def decimals(series: pl.Series, precision: int = 18, scale: int = 2) -> tuple[pl.Series, int]:
    raw = text(series)
    step = Decimal(1).scaleb(-scale)
    limit = Decimal(10) ** (precision - scale)
    readings: dict[str, Decimal] = {}
    for value in raw.drop_nulls().unique().to_list():
        number = parse_number(value)
        # A value too large for the column is unreadable for it, not an error for the load.
        if number is None or not number.is_finite() or abs(number) >= limit:
            continue
        try:
            readings[value] = number.quantize(step, rounding=ROUND_HALF_UP)
        except InvalidOperation:
            continue
    parsed = pl.Series(series.name, [readings.get(value) for value in raw.to_list()],
                       dtype=pl.Decimal(precision, scale))
    invalid = int((raw.is_not_null() & parsed.is_null()).sum())
    return parsed, invalid


def integers(series: pl.Series, dtype=pl.Int32, months: bool = False) -> tuple[pl.Series, int]:
    """Whole numbers; with ``months``, also Jan..Dec / January..December."""
    raw = text(series)
    bound = 2**31 if dtype == pl.Int32 else 2**63
    readings: dict[str, int] = {}
    for value in raw.drop_nulls().unique().to_list():
        number = parse_number(value, percent=False)
        if number is not None and number.is_finite() and number == number.to_integral_value() and abs(number) < bound:
            readings[value] = int(number)
    parsed = pl.Series(series.name, [readings.get(value) for value in raw.to_list()], dtype=dtype)
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

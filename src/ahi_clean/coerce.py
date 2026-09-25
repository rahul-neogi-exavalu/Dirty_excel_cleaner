"""Turn a column of raw cell values into a typed polars Series.

The rule that makes this fast: **the whole column is tried against one strategy at a
time, vectorised, rather than each value being tried against every strategy in Python.**

That is not a micro-optimisation. Profiling the previous implementation showed 51% of
total runtime inside pandas' *scalar* date parser, called once per cell, of which more
than half was pandas re-guessing the format on every single call. The format is a
property of the column, not of the cell, so it should be decided once.

Nothing here knows a field name. Which strategy wins is decided by how much of the
column each one resolves.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

import polars as pl

from . import signals, typing_utils
from .typing_utils import is_blank

DATE_SHARE_THRESHOLD = 0.6
NUMERIC_SHARE_THRESHOLD = 0.6
SAMPLE_HEAD = 200
SAMPLE_CAP = 1000

# A column of same-width, all-different whole numbers could be codes (ZIPs, account and
# centre numbers) or amounts; the values alone cannot say which. With more than this many
# values it is read as a code -- amounts that never repeat and never change width get
# unlikely as a table grows -- and with this many or fewer it is read as a number. Either
# way the column is flagged in the metadata for someone to check against its header.
CODE_MIN_VALUES = 10

# Tried in order. Unambiguous shapes come first so that a column which fits one of them
# is never handed to a pair whose reading depends on convention.
DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d",
    "%d %b %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%d.%m.%Y",
]

# Month-year headings like "Jan-2026" or "2026-03". Deliberately kept out of the column
# ladder above: a cell holding only a month is not a date, and inventing the first of the
# month for it would fabricate precision the source never had. They are recognised here
# because a *pivot axis* of months is a real and common thing to find.
PERIOD_FORMATS = ["%b-%Y", "%b %Y", "%B-%Y", "%B %Y", "%Y-%m", "%m/%Y", "%Y/%m"]

# Genuinely ambiguous: 01/02/2026 is either reading. Resolved per column, never per cell.
AMBIGUOUS_FORMATS = [("%m/%d/%Y", "month-first"), ("%d/%m/%Y", "day-first")]
AMBIGUOUS_DASHED = [("%m-%d-%Y", "month-first"), ("%d-%m-%Y", "day-first")]

# Only genuine *presentation* is stripped: currency marks, thousands separators, percent
# signs and spacing. Deliberately an allowlist of what may be removed, not a blacklist of
# what may stay.
#
# The earlier form deleted everything that was not a digit, which silently turned
# identifiers into numbers: 'POL-1' lost its letters and became -1, and a column of them
# became a column of small negatives. Nothing reported it, because the result was a
# perfectly valid number. Letters now always survive, so a value that is not really
# numeric fails the cast and the column stays text -- which is the correct outcome.
_PRESENTATION = re.compile(r"[\s,_ $£€¥₹%]")
_PARENTHESISED = re.compile(r"^\((.*)\)$")
_EXCEL_EPOCH = _dt.date(1899, 12, 30)
_EXCEL_OFFSET = (_EXCEL_EPOCH - _dt.date(1970, 1, 1)).days

# Two all-digit date shapes, handled apart from the format ladder because a bare number
# is only a date under conditions a format string cannot express.
#
# yyyyMMdd must be exactly eight digits and land in a plausible year: chrono would
# otherwise read a seven-digit id, or year 1005 out of an account number.
COMPACT_DATE = r"^\d{8}$"
COMPACT_FORMAT = "%Y%m%d"
PLAUSIBLE_YEARS = (1900, 2100)
# A five-digit Excel serial day number (46030 is 2026-01-08). A column of nothing but
# five-digit numbers is indistinguishable from ZIPs or codes, so serials are read as
# dates only in a column that already holds dates written some other way.
EXCEL_SERIAL = r"^\d{5}$"


@dataclass
class Coerced:
    """A typed column, with everything a reviewer needs to check the decision."""

    series: pl.Series
    kind: str
    failures: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Set when the type was a judgement call a person should check; None otherwise.
    flag: str | None = None


def sample(values):
    """A representative slice of a column, for decisions that do not need every value.

    A column of a million dates does not need its millionth value to establish that it
    holds dates. The head catches the common case; the stride stops a column that
    changes character half way down from being judged on its opening alone.
    """
    if len(values) <= SAMPLE_CAP:
        return values
    stride = max(1, (len(values) - SAMPLE_HEAD) // (SAMPLE_CAP - SAMPLE_HEAD))
    return values[:SAMPLE_HEAD] + values[SAMPLE_HEAD::stride][: SAMPLE_CAP - SAMPLE_HEAD]


def coerce_column(name: str, values: list) -> Coerced:
    """Decide what a column is, then convert all of it in one pass."""
    present = [value for value in values if not is_blank(value)]
    if not present:
        return Coerced(pl.Series(name, [None] * len(values), dtype=pl.String), typing_utils.EMPTY)

    kind, notes, flag = _decide_kind(name, present)

    if kind in (typing_utils.INT, typing_utils.FLOAT):
        result = _as_number(name, values, notes)
    elif kind in (typing_utils.DATE, typing_utils.DATETIME, typing_utils.DATE_STRING):
        result = _as_date(name, values, notes)
    else:
        result = Coerced(_as_text(name, values), kind, notes=notes)
    result.flag = flag
    return result


# --------------------------------------------------------------------------- #
# Deciding what the column is
# --------------------------------------------------------------------------- #


def _decide_kind(name, present) -> tuple[str, list[str], str | None]:
    sampled = sample(present)
    notes: list[str] = []
    kind = signals.modal_type(sampled)
    types = {typing_utils.infer_type(value) for value in sampled}

    # Checked before the code heuristic: a column of eight-digit yyyyMMdd values is
    # constant-width and near-unique, which is exactly what a code looks like. A column
    # of nothing but numbers must resolve *entirely* -- one value that is not a real
    # calendar date and it is an identifier, not a date.
    if kind == typing_utils.INT:
        texts = _texts(sampled)
        _parsed, _primary, share = _ladder_parse(pl.Series("s", texts, dtype=pl.String))
        numeric_only = all(text is None or text.isdigit() for text in texts)
        if share >= (1.0 if numeric_only else DATE_SHARE_THRESHOLD):
            return typing_utils.DATE, notes, None

    # Letters and digits in one value -- 12AB, A7, X9Y -- is an identifier, never a
    # number. Letting the majority decide instead read such a column as numeric and
    # silently turned every alphanumeric value into an empty cell. Checked on the whole
    # column, because a sampled check would miss the one value that proves it.
    if kind in (typing_utils.INT, typing_utils.FLOAT) and has_alphanumeric(present):
        notes.append(f"column '{name}' kept as text: it holds alphanumeric values")
        return typing_utils.ID_STRING, notes, None

    if kind in (typing_utils.INT, typing_utils.FLOAT):
        shape = code_shape(sampled)
        if shape == LEADING_ZERO:
            notes.append(f"column '{name}' kept as text: a value has a leading zero")
            return typing_utils.ID_STRING, notes, None
        if shape == UNIFORM:
            # Counted on the whole column, not the sample: the sample is capped at 1000.
            if len(present) > CODE_MIN_VALUES:
                flag = (
                    f"CHECK: same-width, all-different whole numbers (code such as ZIP or "
                    f"account number, or an amount?); read as code (String) because it has "
                    f"more than {CODE_MIN_VALUES} values"
                )
                notes.append(f"column '{name}' kept as text: values look like codes")
                return typing_utils.ID_STRING, notes, flag
            flag = (
                f"CHECK: same-width, all-different whole numbers (code such as ZIP or "
                f"account number, or an amount?); read as number (Int64) because it has "
                f"{CODE_MIN_VALUES} or fewer values"
            )
            notes.append(f"column '{name}' read as numbers: too few values to call it a code")
            return typing_utils.INT, notes, flag

    if typing_utils.ID_STRING in types and types & {typing_utils.INT, typing_utils.FLOAT}:
        notes.append(f"column '{name}' mixes identifiers and numbers; kept as text")
        return typing_utils.ID_STRING, notes, None

    # A column written in several date formats has no single lexical signature, so the
    # type lattice sees only "text". Parseability is the honest test.
    if kind in (typing_utils.TEXT, typing_utils.DATE_STRING):
        texts = _texts(sampled)
        _parsed, primary, share = _ladder_parse(pl.Series("s", texts, dtype=pl.String))
        if share >= DATE_SHARE_THRESHOLD:
            # Only say "mixed" when they genuinely are; a clean ISO column reporting
            # mixed formats would send a reviewer looking for a problem that is not there.
            series = pl.Series("s", texts, dtype=pl.String)
            present = series.len() - series.null_count()
            if primary and _resolved_share(series, primary, present) < share:
                notes.append(f"column '{name}' holds several date formats; all parsed as dates")
            return typing_utils.DATE, notes, None

    # Money and accounting negatives are text by shape but numeric in substance.
    if (
        kind == typing_utils.TEXT
        and _numeric_share(_texts(sampled)) >= NUMERIC_SHARE_THRESHOLD
        and not has_alphanumeric(present)
    ):
        notes.append(f"column '{name}' holds formatted numbers; parsed as numeric")
        return typing_utils.FLOAT, notes, None

    return kind, notes, None


def has_alphanumeric(values) -> bool:
    """Whether any value mixes letters and digits, like ``12AB`` or ``A7``.

    Vectorised, because it runs over the whole column rather than a sample. Month-name
    dates never reach it: it only guards the paths that would make a column a number.
    """
    texts = pl.Series("v", _texts(values), dtype=pl.String)
    return bool((texts.str.contains(r"[A-Za-z]") & texts.str.contains(r"\d")).any())


LEADING_ZERO = "leading_zero"
UNIFORM = "uniform"


def code_shape(values) -> str | None:
    """How much a column of whole numbers looks like a code, from its values alone.

    * ``LEADING_ZERO`` -- a value such as ``08085``. Meaningless in a quantity and kept
      only by text, so this settles it: the column is a code.
    * ``UNIFORM`` -- near-unique integers all of one digit width: account numbers, centre
      codes, ZIPs. But whole-dollar amounts that happen never to repeat look the same,
      so this is only a suspicion; the caller decides on size and flags it.
    * ``None`` -- nothing code-like; an ordinary number column.
    """
    texts = [str(value).strip() for value in values]
    digits = [text for text in texts if text.isdigit()]
    if len(digits) < len(texts) or len(digits) < 3:
        return None
    if any(text.startswith("0") for text in digits):
        return LEADING_ZERO
    widths = {len(text) for text in digits}
    if len(widths) == 1 and next(iter(widths)) >= 3 and len(set(digits)) / len(digits) >= 0.9:
        return UNIFORM
    return None


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


def _texts(values) -> list:
    """Normalise raw cells to strings once, so every later pass is vectorised.

    This is the only per-value Python loop that remains, and it is unavoidable: the
    source is a list of heterogeneous cell objects, not a column. It does no parsing.
    """
    out = []
    for value in values:
        if is_blank(value):
            out.append(None)
        elif isinstance(value, _dt.datetime):
            out.append(value.date().isoformat())
        elif isinstance(value, _dt.date):
            out.append(value.isoformat())
        elif isinstance(value, float) and value.is_integer():
            out.append(str(int(value)))
        else:
            out.append(str(value).strip())
    return out


def _as_text(name, values) -> pl.Series:
    return pl.Series(name, _texts(values), dtype=pl.String)


def _numeric_share(texts) -> float:
    series = _clean_numeric(pl.Series("s", texts, dtype=pl.String))
    parsed = series.cast(pl.Float64, strict=False)
    present = sum(1 for text in texts if text is not None)
    return (parsed.len() - parsed.null_count()) / present if present else 0.0


def _clean_numeric(series: pl.Series) -> pl.Series:
    """Strip presentation so the number underneath can be cast.

    Handles currency symbols, thousands separators, percent signs and accounting
    negatives -- ``(1,234.56)`` means minus one thousand two hundred and thirty four,
    and reading it as text loses the sign entirely.
    """
    return (
        series.str.strip_chars()
        .str.replace_all(r"^\((.*)\)$", r"-${1}")
        .str.replace_all(_PRESENTATION.pattern, "")
    )


def _as_number(name, values, notes) -> Coerced:
    texts = _texts(values)
    cleaned = _clean_numeric(pl.Series(name, texts, dtype=pl.String))
    parsed = cleaned.cast(pl.Float64, strict=False)

    failures = [
        {"column": name, "value": str(text)[:80], "reason": "not numeric"}
        for text, result in zip(texts, parsed)
        if text is not None and result is None
    ]

    present = parsed.drop_nulls()
    # A column whose every value is whole is written as an integer. Without this a ZIP
    # or a centre number that escaped the code heuristic lands in the CSV as "75202.0",
    # which is wrong for a downstream load and merely noise to a reader.
    if len(present) and bool((present % 1 == 0).all()):
        return Coerced(parsed.cast(pl.Int64, strict=False), typing_utils.INT, failures, notes)
    return Coerced(parsed, typing_utils.FLOAT, failures, notes)


def looks_like_periods(labels) -> bool:
    """Whether every label names a point in time -- a full date or a month.

    Used to decide what a pivot's column axis represents, which is why months count
    here and do not count as dates elsewhere.
    """
    texts = [None if label is None else str(label).strip() for label in labels]
    series = pl.Series("s", texts, dtype=pl.String)
    present = series.len() - series.null_count()
    if not present:
        return False

    _parsed, _primary, share = _ladder_parse(series)
    if share >= 1.0:
        return True

    remaining = pl.Series("s", texts, dtype=pl.String)
    for fmt in PERIOD_FORMATS:
        if _resolved_share(remaining, fmt, present) >= 1.0:
            return True
    return False


def _ladder_parse(series: pl.Series) -> tuple[pl.Series, str | None, float]:
    """Apply every format in turn, keeping what each one resolves.

    The share that matters is what the ladder resolves *as a whole*, not what its best
    single rung does. A report whose dates arrive in four different formats resolves
    fully here while no individual format covers even a quarter of it -- judging on the
    best single format would call such a column text and lose every date in it.

    The first format to resolve anything is reported as the primary, because that is the
    one whose convention the ambiguity note needs to describe.
    """
    present = series.len() - series.null_count()
    if not present:
        return series.cast(pl.Date, strict=False), None, 0.0

    parsed = pl.Series(series.name, [None] * series.len(), dtype=pl.Date)
    primary = None

    for fmt in _ladder_order(series, present):
        if parsed.null_count() == series.null_count():
            break
        attempt = series.str.to_date(fmt, strict=False)
        if attempt.null_count() == series.len():
            continue
        before = parsed.null_count()
        parsed = parsed.fill_null(attempt)
        if parsed.null_count() < before and primary is None:
            primary = fmt

    before = parsed.null_count()
    parsed = parsed.fill_null(_compact_dates(series))
    if parsed.null_count() < before and primary is None:
        primary = COMPACT_FORMAT

    # Serials only count once something else in the column has proved it holds dates.
    if parsed.null_count() < series.len():
        parsed = parsed.fill_null(excel_serials(series))

    resolved = parsed.len() - parsed.null_count()
    return parsed, primary, resolved / present


def _compact_dates(series: pl.Series) -> pl.Series:
    """yyyyMMdd, for exactly eight digits in a plausible year; null otherwise."""
    low, high = PLAUSIBLE_YEARS
    value = pl.col("v").str.strip_chars()
    date = value.str.to_date(COMPACT_FORMAT, strict=False)
    return pl.DataFrame({"v": series}).select(
        pl.when(value.str.contains(COMPACT_DATE) & date.dt.year().is_between(low, high))
        .then(date)
        .otherwise(None)
    ).to_series()


def excel_serials(series: pl.Series) -> pl.Series:
    """Five-digit Excel serial day numbers as dates (1899-12-30 plus n); null otherwise."""
    value = pl.col("v").str.strip_chars()
    days = pl.when(value.str.contains(EXCEL_SERIAL)).then(value).otherwise(None).cast(pl.Int32)
    # polars stores a Date as days since 1970-01-01, so the Excel epoch is an offset.
    return pl.DataFrame({"v": series}).select(
        (days + _EXCEL_OFFSET).cast(pl.Date)
    ).to_series()


def _ladder_order(series: pl.Series, present: int) -> list[str]:
    """Unambiguous formats first, then the ambiguous pair in its better-fitting order.

    Ordering the ambiguous pair by how much each resolves is what makes the choice a
    property of the column: a column containing 25/12/2026 can only be day-first, and
    that value decides it for the rows where both readings would have been legal.
    """
    order = list(DATE_FORMATS)
    for candidates in (AMBIGUOUS_FORMATS, AMBIGUOUS_DASHED):
        ranked = sorted(
            candidates, key=lambda item: _resolved_share(series, item[0], present), reverse=True
        )
        order.extend(fmt for fmt, _label in ranked)
    return order


def _resolved_share(series: pl.Series, fmt: str, present: int) -> float:
    parsed = series.str.to_date(fmt, strict=False)
    return (parsed.len() - parsed.null_count()) / present


def _as_date(name, values, notes) -> Coerced:
    texts = _texts(values)
    series = pl.Series(name, texts, dtype=pl.String)
    present = series.len() - series.null_count()
    if not present:
        return Coerced(series, typing_utils.DATE, notes=notes)

    parsed, chosen, share = _ladder_parse(series)
    if chosen is None:
        failures = [
            {"column": name, "value": str(text)[:80], "reason": "unparseable date"}
            for text in texts
            if text is not None
        ]
        return Coerced(_as_text(name, values), typing_utils.TEXT, failures, notes)

    notes = list(notes)
    _note_ambiguity(name, series, chosen, present, share, notes)

    failures = [
        {"column": name, "value": str(text)[:80], "reason": "unparseable date"}
        for text, result in zip(texts, parsed)
        if text is not None and result is None
    ]
    # Kept as a real Date so the frame's schema says what the column is: the metadata
    # reads its datatype from here. write_csv renders a Date as YYYY-MM-DD.
    return Coerced(parsed.alias(name), typing_utils.DATE, failures, notes)


def _note_ambiguity(name, series, chosen, present, share, notes) -> None:
    """Record when a column's date order was a judgement call rather than a reading.

    ``01/02/2026`` is either the first of February or the second of January. The choice
    is made once for the whole column and said out loud, because a column that silently
    mixes both conventions is exactly the error this pipeline exists to surface.
    """
    for fmt, label in AMBIGUOUS_FORMATS + AMBIGUOUS_DASHED:
        if fmt != chosen:
            continue
        other = next(
            candidate
            for candidate, _ in AMBIGUOUS_FORMATS + AMBIGUOUS_DASHED
            if candidate != chosen and candidate[1] == chosen[1]
        )
        if abs(_resolved_share(series, other, present) - share) < 1e-9:
            notes.append(
                f"column '{name}' dates are ambiguous ({chosen} and {other} both fit); "
                f"read as {label}"
            )
        else:
            notes.append(f"column '{name}' dates read as {label} ({chosen})")

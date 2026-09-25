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

from . import flags as flag_text
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

# Header words that name a date. Used for ONE decision only: a column of nothing but
# five-digit numbers, where the values genuinely cannot say ZIP code or Excel date serial
# (46030 is both an Indiana ZIP and 2026-01-08). Everywhere else the values decide and the
# header is never read. A hint never goes unannounced: the column is flagged CHECK
# whichever way it goes.
#
# Conventions covered: dbt and GitLab name timestamps <event>_at and dates <event>_date,
# Oracle uses _dt and _dttm, _on marks date-only columns, _ts timestamps.
#
# Matched anywhere in the header, like SQL's LIKE '%date%': long, distinctive fragments.
DATE_HEADER_SUBSTRINGS = (
    "date", "dt", "time", "day", "period", "expir", "effectiv", "matur", "birth",
    "fecha", "datum", "giorno", "tarih",
)
# Matched only as a whole word of the header, split on separators and camelCase. As
# substrings these are ruinous: "at" alone is inside rate, state, status, category,
# format, vat, latitude and location.
DATE_HEADER_WORDS = {
    "at", "on", "ts", "dob", "doj", "when", "since", "until", "due", "asof", "eff",
    "tag", "dia", "jour",
}
# Event words that are dates -- created, updated -- unless the header also marks a person
# or an identifier: created_by and updated_by hold user ids, which can be 5-digit numbers.
DATE_EVENT_WORDS = {"created", "updated", "modified", "posted", "issued", "closed", "opened"}
NOT_A_DATE_MARKERS = {"by", "id", "user", "no", "num", "number", "code", "count"}
# Ordinary words that contain a date fragment. Removed before the substring search, so
# width is not %dt%, lifetime is not %time% and update_count is not %date%.
NOT_DATE_FRAGMENTS = (
    "bandwidth", "width", "breadth", "hundredth", "thousandth",
    "lifetime", "overtime", "runtime", "downtime", "uptime", "timeout", "anytime",
    "update", "candidate", "validate", "mandate", "accommodate", "consolidate",
    "liquidate", "sedate", "intimidate",
)


@dataclass
class Coerced:
    """A typed column, with everything a reviewer needs to check the decision."""

    series: pl.Series
    kind: str
    failures: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Edge cases for a person to know about -- see flags.py for CHECK versus INFO.
    flags: list[str] = field(default_factory=list)


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


def coerce_column(name: str, values: list, label=None) -> Coerced:
    """Decide what a column is, then convert all of it in one pass.

    ``label`` is the header exactly as the sheet wrote it (``CreatedAt``, ``Txn Dt``). It
    is consulted for one tie only -- see ``DATE_HEADER_WORDS``.
    """
    present = [value for value in values if not is_blank(value)]
    if not present:
        return Coerced(pl.Series(name, [None] * len(values), dtype=pl.String), typing_utils.EMPTY)

    serial_like = _serial_candidates(present)
    header = str(label) if label is not None else name
    if serial_like and header_names_a_date(header):
        result = _as_date(name, values, [], serials=True)
        if result.series.dtype == pl.Date:
            example = f"{serial_like[0]} = {result.series.drop_nulls()[0].isoformat()}"
            result.flags.insert(0, flag_text.check(
                f"every value is a 5-digit number (ZIP code or Excel date serial?); read "
                f"as dates because the header '{header}' names a date (e.g. {example}); "
                "confirm it is not a ZIP or code"
            ))
            result.notes.append(f"column '{name}' read as Excel date serials: header names a date")
            return result

    kind, notes, flags = _decide_kind(name, present)

    if kind in (typing_utils.INT, typing_utils.FLOAT):
        result = _as_number(name, values, notes)
    elif kind in (typing_utils.DATE, typing_utils.DATETIME, typing_utils.DATE_STRING):
        result = _as_date(name, values, notes)
    else:
        result = Coerced(_as_text(name, values), kind, notes=notes)
    # What decided the type first, then what happened to the values while converting.
    result.flags = flags + result.flags
    if serial_like:
        read_as = "number (Int64)" if result.series.dtype == pl.Int64 else "text (String)"
        result.flags.append(flag_text.check(
            "every value is a 5-digit number: could be ZIP codes, amounts or Excel date "
            f"serials (e.g. {serial_like[0]} would be "
            f"{excel_serials(pl.Series([serial_like[0]]))[0].isoformat()}); the header "
            f"'{header}' does not name a date, so kept as {read_as}"
        ))
    return result


def _serial_candidates(present) -> list[str]:
    """The values, when every one is a 5-digit number that is a plausible date serial."""
    texts = [text for text in _texts(present) if text is not None]
    if not texts or not all(len(text) == 5 and text.isdigit() for text in texts):
        return []
    dates = excel_serials(pl.Series(texts))
    low, high = PLAUSIBLE_YEARS
    return texts if bool(dates.dt.year().is_between(low, high).all()) else []


def header_words(header: str) -> list[str]:
    """A header split into lower-case words: on separators, and on camelCase."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(header))
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word]


def header_names_a_date(header: str) -> bool:
    """Whether a header reads as a date.

    Three ways in: a date fragment anywhere (``%date%``, ``%dt%``, ``%time%`` ...) once
    ordinary words containing one are set aside; a short date word standing on its own
    (``created_at``, ``Posted On``); or an event word with no person or id beside it.
    """
    text = str(header).lower()
    for fragment in NOT_DATE_FRAGMENTS:
        text = text.replace(fragment, " ")
    if any(fragment in text for fragment in DATE_HEADER_SUBSTRINGS):
        return True

    words = set(header_words(header))
    if words & DATE_HEADER_WORDS:
        return True
    return bool(words & DATE_EVENT_WORDS) and not words & NOT_A_DATE_MARKERS


# --------------------------------------------------------------------------- #
# Deciding what the column is
# --------------------------------------------------------------------------- #


def _decide_kind(name, present) -> tuple[str, list[str], list[str]]:
    """The column's type, the notes for the audit, and the flags for a person."""
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
            flags = []
            if numeric_only:
                flags.append(flag_text.check(
                    "every value is an 8-digit number that is a valid yyyyMMdd date; "
                    "read as dates (could be identifiers)"
                ))
            return typing_utils.DATE, notes, flags

    # Letters and digits in one value -- 12AB, A7, X9Y -- is an identifier, never a
    # number. Letting the majority decide instead read such a column as numeric and
    # silently turned every alphanumeric value into an empty cell. Checked on the whole
    # column, because a sampled check would miss the one value that proves it.
    if kind in (typing_utils.INT, typing_utils.FLOAT):
        mixed = alphanumeric_values(present)
        if mixed:
            notes.append(f"column '{name}' kept as text: it holds alphanumeric values")
            return typing_utils.ID_STRING, notes, [flag_text.check(
                f"mostly numbers, but {len(mixed)} value(s) mix letters and digits "
                f"({flag_text.example(mixed)}); whole column kept as text (String)"
            )]

    if kind in (typing_utils.INT, typing_utils.FLOAT):
        shape = code_shape(sampled)
        if shape == LEADING_ZERO:
            notes.append(f"column '{name}' kept as text: a value has a leading zero")
            return typing_utils.ID_STRING, notes, [flag_text.info(
                "numbers with leading zeros (e.g. 08085); kept as text so the zeros survive"
            )]
        if shape == UNIFORM:
            # Counted on the whole column, not the sample: the sample is capped at 1000.
            if len(present) > CODE_MIN_VALUES:
                notes.append(f"column '{name}' kept as text: values look like codes")
                return typing_utils.ID_STRING, notes, [flag_text.check(
                    "same-width, all-different whole numbers (code such as ZIP or account "
                    "number, or an amount?); read as code (String) because it has more "
                    f"than {CODE_MIN_VALUES} values"
                )]
            notes.append(f"column '{name}' read as numbers: too few values to call it a code")
            return typing_utils.INT, notes, [flag_text.check(
                "same-width, all-different whole numbers (code such as ZIP or account "
                "number, or an amount?); read as number (Int64) because it has "
                f"{CODE_MIN_VALUES} or fewer values"
            )]

    if typing_utils.ID_STRING in types and types & {typing_utils.INT, typing_utils.FLOAT}:
        notes.append(f"column '{name}' mixes identifiers and numbers; kept as text")
        return typing_utils.ID_STRING, notes, [flag_text.check(
            "mixes identifiers (like POL-1) with plain numbers; whole column kept as text"
        )]

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
            flags = []
            if primary and _resolved_share(series, primary, present) < share:
                notes.append(f"column '{name}' holds several date formats; all parsed as dates")
                flags.append(flag_text.info(
                    "dates written in several formats; all normalised to YYYY-MM-DD"
                ))
            return typing_utils.DATE, notes, flags

    # Money and accounting negatives are text by shape but numeric in substance.
    if (
        kind == typing_utils.TEXT
        and _numeric_share(_texts(sampled)) >= NUMERIC_SHARE_THRESHOLD
        and not alphanumeric_values(present)
    ):
        notes.append(f"column '{name}' holds formatted numbers; parsed as numeric")
        return typing_utils.FLOAT, notes, _formatting_flags(_texts(present))

    return kind, notes, []


def _formatting_flags(texts) -> list[str]:
    """What presentation was stripped to read formatted numbers, and what it cost."""
    present = [text for text in texts if text is not None]
    found = []
    if any(re.search(r"[$£€¥₹]", text) for text in present):
        found.append("currency symbols")
    if any("," in text for text in present):
        found.append("thousands separators")
    if any(_PARENTHESISED.match(text.strip()) for text in present):
        found.append("brackets (read as negatives, e.g. (500) = -500)")
    flags = [flag_text.info(f"formatted numbers; removed {', '.join(found)}")] if found else []
    if any("%" in text for text in present):
        flags.append(flag_text.check(
            "percent signs removed: 99% is stored as 99, not 0.99"
        ))
    return flags


def alphanumeric_values(values) -> list[str]:
    """Every value that mixes letters and digits, like ``12AB`` or ``A7``.

    Vectorised, because it runs over the whole column rather than a sample. Month-name
    dates never reach it: it only guards the paths that would make a column a number.
    """
    texts = pl.Series("v", _texts(values), dtype=pl.String)
    mask = texts.str.contains(r"[A-Za-z]") & texts.str.contains(r"\d")
    return texts.filter(mask.fill_null(False)).to_list()


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

    flags = []
    if failures:
        flags.append(flag_text.check(
            f"{len(failures)} value(s) are not numbers and were left empty "
            f"({flag_text.example(item['value'] for item in failures)})"
        ))

    present = parsed.drop_nulls()
    # A column whose every value is whole is written as an integer. Without this a ZIP
    # or a centre number that escaped the code heuristic lands in the CSV as "75202.0",
    # which is wrong for a downstream load and merely noise to a reader.
    if len(present) and bool((present % 1 == 0).all()):
        return Coerced(parsed.cast(pl.Int64, strict=False), typing_utils.INT, failures, notes, flags)
    return Coerced(parsed, typing_utils.FLOAT, failures, notes, flags)


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


def _ladder_parse(series: pl.Series, serials: bool = False) -> tuple[pl.Series, str | None, float]:
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

    # Serials only count once something else in the column has proved it holds dates --
    # or when the caller has already settled that the column is serials.
    if serials or parsed.null_count() < series.len():
        before = parsed.null_count()
        parsed = parsed.fill_null(excel_serials(series))
        if parsed.null_count() < before and primary is None:
            primary = "excel_serial"

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


def _as_date(name, values, notes, serials: bool = False) -> Coerced:
    texts = _texts(values)
    series = pl.Series(name, texts, dtype=pl.String)
    present = series.len() - series.null_count()
    if not present:
        return Coerced(series, typing_utils.DATE, notes=notes)

    parsed, chosen, share = _ladder_parse(series, serials=serials)
    if chosen is None:
        failures = [
            {"column": name, "value": str(text)[:80], "reason": "unparseable date"}
            for text in texts
            if text is not None
        ]
        return Coerced(_as_text(name, values), typing_utils.TEXT, failures, notes, [
            flag_text.check("looked like dates, but none could be read; kept as text")
        ])

    notes = list(notes)
    flags = _date_order(name, series, present, notes)

    failures = [
        {"column": name, "value": str(text)[:80], "reason": "unparseable date"}
        for text, result in zip(texts, parsed)
        if text is not None and result is None
    ]
    if failures:
        flags.append(flag_text.check(
            f"{len(failures)} value(s) could not be read as dates and were left empty "
            f"({flag_text.example(item['value'] for item in failures)})"
        ))
    if not serials:
        # When serials alone decided the column, the CHECK already says so.
        flags.extend(_date_conversion_flags(values, texts, parsed, present))

    # Kept as a real Date so the frame's schema says what the column is: the metadata
    # reads its datatype from here. write_csv renders a Date as YYYY-MM-DD.
    return Coerced(parsed.alias(name), typing_utils.DATE, failures, notes, flags)


_TIME_OF_DAY = re.compile(r"\d{1,2}:\d{2}")


def _date_conversion_flags(values, texts, parsed, present) -> list[str]:
    """What turning these values into plain dates changed: serials, compact dates, times."""
    flags = []
    resolved = [(text, date) for text, date in zip(texts, parsed) if text is not None and date is not None]

    serials = [(text, date) for text, date in resolved if re.fullmatch(EXCEL_SERIAL, text)]
    if serials:
        text, date = serials[0]
        flags.append(flag_text.info(
            f"{len(serials)} Excel serial number(s) converted to dates "
            f"(e.g. {text} = {date.isoformat()})"
        ))

    compact = [text for text, _date in resolved if re.fullmatch(COMPACT_DATE, text)]
    # A column made only of them is already flagged as a judgement call when typed.
    if compact and len(compact) < present:
        flags.append(flag_text.info(
            f"{len(compact)} value(s) written as yyyyMMdd (e.g. {compact[0]}) read as dates"
        ))

    timed = [
        value for value in values
        if (isinstance(value, _dt.datetime) and value.time() != _dt.time(0, 0))
        or (isinstance(value, str) and _TIME_OF_DAY.search(value))
    ]
    if timed:
        flags.append(flag_text.info(
            f"time of day dropped from {len(timed)} value(s) "
            f"(e.g. {flag_text.example(timed, 1)}); only the date is kept"
        ))
    return flags


def _date_order(name, series, present, notes) -> list[str]:
    """Say which way round slash or dash dates were read, and whether it was a guess.

    ``01/02/2026`` is either the first of February or the second of January. The choice
    is made once for the whole column and said out loud, because a column that silently
    mixes both conventions is exactly the error this pipeline exists to surface. When
    both readings fit every value, nothing in the data decided it: that is a CHECK.
    """
    flags = []
    for pair in (AMBIGUOUS_FORMATS, AMBIGUOUS_DASHED):
        shares = [(_resolved_share(series, fmt, present), fmt, label) for fmt, label in pair]
        if not any(share for share, _fmt, _label in shares):
            continue
        # The ladder tries the better-fitting reading first; on a tie, month-first.
        (first_share, first_fmt, first_label), (second_share, second_fmt, _label) = sorted(
            shares, key=lambda item: item[0], reverse=True
        )
        if abs(first_share - second_share) < 1e-9:
            notes.append(
                f"column '{name}' dates are ambiguous ({first_fmt} and {second_fmt} both "
                f"fit); read as {first_label}"
            )
            flags.append(flag_text.check(
                f"dates such as 01/02/2026 fit both month-first and day-first; read as "
                f"{first_label} ({first_fmt}), so confirm against the source"
            ))
        else:
            notes.append(f"column '{name}' dates read as {first_label} ({first_fmt})")
            flags.append(flag_text.info(
                f"dates read as {first_label} ({first_fmt}): only that reading fits every value"
            ))
    return flags

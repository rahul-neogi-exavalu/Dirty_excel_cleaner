"""Is a cleaned file fit for Bronze? The business's rules, with no I/O.

1. **Required columns.** Every file must carry the columns listed in
   ``backend/config/bronze_required_columns.csv`` (Silver catalog names; which file
   column is which is the column mapping's job). Columns sharing a ``one_of`` name are
   alternatives: one of them is enough, and more are fine (CommissionPct or
   GrossCommissionAmount). One requirement missing: the file is rejected.
2. **Reporting dates.** The accounting effective date (AED) decides them if it is
   populated -- a readable date -- on every row; else the transaction effective date
   (TED) on every row; else the policy effective date (PED). 99 rows of 100 is not every
   row. The reviewer may pick another date column populated on every row (its range is
   YTD or monthly where the first's is not); that column is the file's ``date_detail``.
   When none is, a person enters the dates: ``date_detail`` is the date column whose
   range they match, else blank. They are whole months: the 1st of the first month to
   the last day of the last.
3. **YTD or monthly.** A file from January to some month of one year is year-to-date; a
   file of one month is monthly. Anything else (Feb-Jun, or 2024-2026) is flagged: the
   reviewer may correct the dates, and a file left flagged is rejected.
4. **What the control table does.** Against the profit center's files already in Bronze
   that year (the profit center read from the file name): a year-to-date file is inserted
   and replaces them; a monthly file for a month not loaded yet is appended -- into the
   same bronze table as the files it continues; a monthly file for a month already loaded
   is flagged and rejected. A file with exactly the reporting dates of one
   already loaded is the reviewer's call too: a **revision** replaces the file the
   reviewer names (looked up among the loaded files of those dates), a **companion**
   came with it and is kept beside it (joined in Silver), or it is rejected.
5. **A file is fit whole, but for sheets that are not data.** A sheet (cleaned table)
   missing required columns -- a lookup or notes sheet beside the data -- is left out on
   its own, and the file's other sheets go ahead. Any other sheet not fit for Bronze
   (flagged dates, rejected by the reviewer) rejects every sheet of that file.
"""

from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl

AED, PED, TED = "AED", "PED", "TED"
DATE_ORDER = (AED, TED, PED)
YTD, MONTHLY = "YTD", "MONTHLY"
INSERT, APPEND, REJECTED = "INSERT", "APPEND", "REJECTED"
# Not an action the control table records: the reviewer has to choose first.
DECIDE = "DECIDE"
# The reviewer's choices.
REJECT, REPLACE, REPLACE_MONTH = "reject", "replace", "replace_month"
# A revised file replacing the one the reviewer names; a companion kept beside its primary.
REVISE, COMPANION = "revise", "companion"


@dataclass(frozen=True)
class Required:
    name: str  # Silver catalog name
    label: str  # as the business writes it
    date_role: str | None = None  # AED / PED / TED
    # Required columns sharing it are alternatives: the file needs one of them.
    one_of: str | None = None


def load_required(path) -> list[Required]:
    with open(Path(path), encoding="utf-8", newline="") as handle:
        return [
            Required(row["silver_column_name"].strip(), (row.get("label") or row["silver_column_name"]).strip(),
                     (row.get("date_role") or "").strip().upper() or None,
                     (row.get("one_of") or "").strip().casefold() or None)
            for row in csv.DictReader(handle) if (row.get("silver_column_name") or "").strip()
        ]


def requirements(items) -> list[tuple[Required, ...]]:
    """What a file must carry, in order: each column on its own, or the alternatives of a
    ``one_of`` group together."""
    groups: dict[str, list[Required]] = {}
    found: list[list[Required]] = []
    for item in items:
        if item.one_of is None:
            found.append([item])
        elif item.one_of in groups:
            groups[item.one_of].append(item)
        else:
            groups[item.one_of] = [item]
            found.append(groups[item.one_of])
    return [tuple(group) for group in found]


def requirement_label(group: tuple[Required, ...]) -> str:
    return " or ".join(item.label for item in group)


def missing_required(items, present: set[str]) -> list[str]:
    """The requirements with none of their columns in ``present`` (Silver names), as labels:
    ``Revenue``, ``CommissionPct or GrossCommissionAmount``."""
    return [requirement_label(group) for group in requirements(items)
            if not any(item.name in present for item in group)]


# --- reporting dates ---------------------------------------------------------------


@dataclass
class DateStat:
    """How well one of AED / PED / TED is populated."""

    role: str
    column: str | None  # the file column mapped to it; None when unmapped
    rows: int = 0
    populated: int = 0  # readable dates
    invalid: int = 0  # values present but not a date
    first: date | None = None
    last: date | None = None

    @property
    def complete(self) -> bool:
        return self.column is not None and self.rows > 0 and self.populated == self.rows

    @property
    def percent(self) -> float:
        return round(100 * self.populated / self.rows, 2) if self.rows else 0.0

    def as_dict(self) -> dict:
        return {"role": self.role, "column": self.column, "rows": self.rows, "populated": self.populated,
                "invalid": self.invalid, "percent": self.percent, "complete": self.complete,
                "first": self.first.isoformat() if self.first else None,
                "last": self.last.isoformat() if self.last else None}


def parse_dates(series: pl.Series) -> pl.Series:
    """The values as dates: the Silver formats (yyyy-MM-dd, MM/dd/yyyy, ...); unreadable -> null."""
    from ahi_silver.cleanse import dates

    if series.dtype == pl.Date:
        return series
    if isinstance(series.dtype, pl.Datetime):
        return series.dt.date()
    return dates(series)[0]


def date_stats(frame: pl.DataFrame, columns: dict[str, str | None]) -> dict[str, DateStat]:
    """``columns``: AED / PED / TED -> the frame column mapped to it (or None)."""
    stats = {}
    for role in DATE_ORDER:
        column = columns.get(role)
        stat = DateStat(role, column, rows=frame.height)
        if column is not None and column in frame.columns:
            raw = frame[column]
            parsed = parse_dates(raw)
            present = raw.cast(pl.String).str.strip_chars().replace("", None).is_not_null()
            stat.populated = int(parsed.is_not_null().sum())
            stat.invalid = int((present & parsed.is_null()).sum())
            if stat.populated:
                stat.first, stat.last = parsed.min(), parsed.max()
        elif column is not None:
            stat.column = None
        stats[role] = stat
    return stats


def reporting_source(stats: dict[str, DateStat]) -> DateStat | None:
    """AED, else TED, else PED: the first populated on every row."""
    return next((stats[role] for role in DATE_ORDER if role in stats and stats[role].complete), None)


def role_range(stat: DateStat) -> tuple[date, date]:
    """The whole months a date column spans."""
    return month_start(stat.first), month_end(stat.last)


def role_ranges(stats: dict[str, DateStat]) -> dict[str, dict]:
    """Every date column populated on every row -- the ones that may decide the reporting
    dates -- with its whole-month range and whether that is YTD, monthly, or neither."""
    ranges = {}
    for role in DATE_ORDER:
        stat = stats.get(role)
        if stat is None or not stat.complete:
            continue
        start, end = role_range(stat)
        kind, flag = classify(start, end)
        ranges[role] = {"start": start, "end": end, "period_type": kind, "flag": flag}
    return ranges


def matching_role(stats: dict[str, DateStat], start: date, end: date) -> str | None:
    """The date column, in priority order, populated on every row whose range is exactly
    these whole months: what dates a person entered came from, if any did."""
    return next((role for role in DATE_ORDER if role in stats and stats[role].complete
                 and role_range(stats[role]) == (start, end)), None)


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def classify(start: date, end: date) -> tuple[str | None, str | None]:
    """(YTD or MONTHLY, None), or (None, why the dates fit neither)."""
    if (start.year, start.month) == (end.year, end.month):
        return MONTHLY, None
    if start.year != end.year:
        return None, (f"The dates run from {start:%b %Y} to {end:%b %Y}, across years. Enter this file's "
                      "reporting dates, or it is rejected.")
    if start.month == 1:
        return YTD, None
    return None, (f"{start:%b}–{end:%b %Y} is neither year-to-date (from January) nor a single month. "
                  "Enter this file's reporting dates, or it is rejected.")


def row_months(frame: pl.DataFrame, column: str | None, fixed: str | None = None,
               sheets: tuple[str, dict[str, str]] | None = None) -> pl.Series:
    """Each row's month (YYYY-MM): from ``column``'s dates, from the month its sheet is
    (``sheets``: the sheet column and sheet -> month), or ``fixed`` for every row."""
    if column is not None and column in frame.columns:
        return parse_dates(frame[column]).dt.strftime("%Y-%m").alias("_reporting_month")
    if sheets and sheets[0] in frame.columns:
        return frame[sheets[0]].cast(pl.String).replace_strict(sheets[1], default=None, return_dtype=pl.String) \
            .alias("_reporting_month")
    return pl.Series("_reporting_month", [fixed] * frame.height, dtype=pl.String)


def sheet_month(name: str) -> str | None:
    """The one month (YYYY-MM) a sheet's name states -- "January 2026", "Jun 2026" -- if
    it states exactly one month of one year."""
    from .period_tokens import period_months, years_in

    months, years = period_months(name or ""), set(years_in(name or ""))
    if months is None or months[0] != months[1] or len(years) != 1:
        return None
    return f"{years.pop():04d}-{months[0]:02d}"


def sheet_months(names) -> dict[str, str] | None:
    """Each sheet -> its month, when every sheet names one month: a summary sent a month
    to a sheet."""
    found = {name: sheet_month(name) for name in dict.fromkeys(names) if name}
    if not found or any(month is None for month in found.values()):
        return None
    return found


def month_stats(frame: pl.DataFrame, months: pl.Series, columns: dict[str, str | None]) -> dict[str, dict]:
    """Per month (YYYY-MM): rows, and the % of them with a readable AED / PED / TED."""
    data = {"month": months}
    for role in DATE_ORDER:
        column = columns.get(role)
        if column is not None and column in frame.columns:
            data[role] = parse_dates(frame[column]).is_not_null()
    table = pl.DataFrame(data).filter(pl.col("month").is_not_null())
    if table.is_empty():
        return {}
    roles = [role for role in DATE_ORDER if role in data]
    grouped = table.group_by("month").agg(
        [pl.len().alias("rows")] + [(pl.col(role).mean() * 100).round(2).alias(role) for role in roles])
    result = {}
    for row in grouped.sort("month").iter_rows(named=True):
        result[row["month"]] = {"rows": row["rows"], **{role: row.get(role) for role in DATE_ORDER}}
    return result


# --- what the control table does ----------------------------------------------------


@dataclass(frozen=True)
class Loaded:
    """A file of this profit center already in Bronze (control row loaded and active)."""

    control_id: int
    file_name: str
    period_type: str | None
    start: date
    end: date
    rows: int | None = None
    columns: int | None = None
    months: dict = field(default_factory=dict, hash=False, compare=False)
    # The file received date, the date column its dates are the range of, and the required
    # columns (Silver names) it carries: what a companion arriving later needs of it.
    received: date | None = field(default=None, compare=False)
    date_detail: str | None = field(default=None, compare=False)
    present: frozenset = field(default_factory=frozenset, compare=False)
    # Its bronze columns, in order (what a file appended to it is compared with), and
    # whether it holds the profit center's own aggregates rather than transactions.
    column_names: tuple = field(default=(), compare=False)
    aggregated: bool = field(default=False, compare=False)

    def label(self) -> str:
        return f"{self.file_name} ({_span(self.start, self.end)})"


@dataclass
class Decision:
    action: str  # INSERT / APPEND / REJECTED / DECIDE
    reasons: list[str] = field(default_factory=list)
    replaces: list[Loaded] = field(default_factory=list)  # whole earlier files this one supersedes
    overlaps: list[Loaded] = field(default_factory=list)  # earlier files holding this month
    replace_month: str | None = None  # YYYY-MM removed from the overlapping files
    confirm: bool = False  # the reviewer confirms it when ingesting
    options: list[str] = field(default_factory=list)  # for DECIDE
    # Earlier files this one continues in their bronze table (a month appended after them).
    appends: list[Loaded] = field(default_factory=list)
    # Rejected for a reason the reviewer is shown prominently (a month loaded twice).
    flagged: bool = False

    def file_replaced(self) -> str | None:
        files = self.replaces or (self.overlaps if self.replace_month else [])
        return ", ".join(dict.fromkeys(item.file_name for item in files)) or None


def overlapping(loaded: list[Loaded], start: date, end: date) -> list[Loaded]:
    """The loaded files whose reporting dates share a month with these: the ones a revision
    of this period may name."""
    return [item for item in loaded if item.start <= end and start <= item.end]


def business_action(*, missing: list[str], flag: str | None, period_type: str | None,
                    start: date | None, end: date | None, loaded: list[Loaded],
                    choice: str | None = None, revises: Loaded | None = None,
                    companion: str | None = None, partners: bool = False, pair: str | None = None) -> Decision:
    """What the control table records for a file (or DECIDE: the reviewer chooses first).

    ``loaded``: this profit center's files in Bronze -- control rows loaded and active.
    ``choice``: the reviewer's ``reject``, ``replace`` (an older year-to-date file),
    ``replace_month`` (a month already loaded), ``revise`` (with ``revises``: the loaded
    file it replaces, as the reviewer named it) or ``companion`` (with ``companion``: the
    file it came with, whose reporting dates it takes).
    ``missing``: for a pair, what neither file carries; ``pair`` names the two.
    ``partners``: files of this profit center came with it, so it may be one's companion.
    """
    if choice == REJECT:
        return Decision(REJECTED, ["Rejected by the reviewer."])
    if missing:
        across = f" (across {pair})" if pair else ""
        reasons = [f"Missing required column{'s' if len(missing) > 1 else ''}: {', '.join(missing)}{across}."]
        if partners and choice != COMPANION:
            # A list beside the data (a broker list) carries few of them on its own.
            reasons.append("If it came with another file of this profit center, make it that file's companion: "
                           "the two are checked together and joined in Silver.")
            return Decision(REJECTED, reasons, options=[COMPANION])
        return Decision(REJECTED, reasons)
    if choice == COMPANION and companion:
        return Decision(APPEND, [f"Companion of {companion}: kept beside it, with its reporting dates, and joined "
                                 "with it in Silver."])
    if flag:
        return Decision(REJECTED, [flag])
    if period_type is None or start is None or end is None:
        raise ValueError("business_action needs the reporting dates")

    same_year = [item for item in loaded if item.end.year == end.year]
    if choice == REVISE and revises is not None:
        later = [item for item in same_year if item.start > end and item != revises]
        reasons = [f"Revision: replaces {revises.label()}."]
        if later:
            reasons.append(f"Keeps the later {', '.join(item.label() for item in later)}.")
        return Decision(INSERT, reasons, replaces=[revises], confirm=True)
    # Exactly the reporting dates of a file already loaded: a revision of it, or a file
    # that came with it. Only the reviewer can tell.
    same = [item for item in loaded if (item.start, item.end) == (start, end)]
    if same:
        return Decision(DECIDE, [f"{', '.join(item.file_name for item in same)} already {'has' if len(same) == 1 else 'have'} "
                                 f"{_span(start, end)}. A revision replaces the file you name; a companion came with "
                                 "it and is kept beside it, joined in Silver."],
                        overlaps=same, options=[REVISE, COMPANION, REJECT])
    if period_type == YTD:
        if not same_year:
            return Decision(INSERT, [f"First file of {end.year} for this profit center."])
        # The year's files this one covers are replaced; later months (July after a
        # Jan-Jun file) stay. A file reaching past this one (Jan-Aug after Jan-Jun) cannot
        # be cut: replacing it loses its later months, so the reviewer decides.
        covered = [item for item in same_year if item.end <= end]
        later = [item for item in same_year if item.start > end]
        across = [item for item in same_year if item not in covered and item not in later]
        if across and choice != REPLACE:
            return Decision(DECIDE, [f"{', '.join(item.label() for item in across)} already runs past "
                                     f"{end:%b %Y}: this file is older than what is in Bronze. Replacing it "
                                     "loses the months after."],
                            replaces=covered + across, options=[REPLACE, REJECT])
        replaced = covered + across
        reasons = [f"Year to date: replaces {', '.join(item.label() for item in replaced)}."] if replaced else [
            f"Year to date: nothing loaded for {start:%b}–{end:%b %Y} yet."]
        if later:
            reasons.append(f"Keeps the later {', '.join(item.label() for item in later)}.")
        return Decision(INSERT, reasons, replaces=replaced, confirm=bool(replaced))

    month = month_key(start)
    holding = [item for item in same_year if item.start <= start and end <= item.end]
    if holding:
        # A month is loaded once: the control table rejects it, and the reviewer is told.
        holders = ", ".join(item.label() for item in holding)
        return Decision(REJECTED, [f"{start:%b %Y} is already loaded from {holders}: a month already in Bronze is "
                                   "not loaded again, so this file is rejected."], overlaps=holding, flagged=True)
    if not same_year:
        return Decision(INSERT, [f"First file of {end.year} for this profit center."])
    reasons = [f"Adds {start:%b %Y} to {end.year}'s files: appended in the same bronze table as "
               f"{', '.join(item.file_name for item in same_year)}."]
    covered = {key for item in same_year for key in _months_between(item.start, item.end)}
    gap = [key for key in _months_between(date(end.year, 1, 1), month_start(start)) if key not in covered and key != month]
    if gap:
        reasons.append(f"Not received yet: {', '.join(_month_label(key) for key in gap)}.")
    return Decision(APPEND, reasons, appends=same_year)


def rejected_with_file(unfit: dict[str, str]) -> Decision:
    """A sheet whose file has other data sheets not fit for Bronze. ``unfit``: each of those
    sheets -> why it is rejected."""
    named = "; ".join(f"{sheet} ({why.rstrip('.')})" for sheet, why in unfit.items())
    return Decision(REJECTED, [f"Rejected with its file: every data sheet of a file must be fit for Bronze, and "
                               f"{named} {'is' if len(unfit) == 1 else 'are'} not."])


def _months_between(start: date, end: date) -> list[str]:
    keys, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def _month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{calendar.month_abbr[int(month)]} {year}"


def _span(start: date, end: date) -> str:
    return f"{start:%b %Y}" if (start.year, start.month) == (end.year, end.month) else f"{start:%b}–{end:%b %Y}"

"""Bronze table names: ``ext_[source_system]_[sheet_name]``.

A sheet named after a month or date (Jan, Feb, June, 2024-07, Q1, H1, Week 32 ...) would
bake the period into the table name, so those tables get the generic
``ext_[source_system]_data`` instead, and the next month's file lands in the same table.
What counts as a period is decided by ``period_tokens``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import period_tokens

PREFIX = "ext"
GENERIC = "data"
# Postgres truncates identifiers longer than this.
MAX_IDENTIFIER = 63


def has_period(text: str, years: set[int] | None = None) -> bool:
    """Whether a sheet (or file) name carries a month, date, quarter, half, week or year
    (a bare year only near ``years``, the file's own, when they are known)."""
    return period_tokens.has_period(text, years)


def slug(text: str) -> str:
    """Lower-case snake_case, safe as an unquoted SQL identifier fragment."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").casefold()).strip("_")


def source_system_from_filename(filename: str, pattern: str) -> str | None:
    """The source system the team suffixed to the file name, e.g. ``ARR_pc0515`` -> ``pc0515``.

    The last match in the stem wins, so ``pc_002_multisheet`` still yields ``pc002``. A
    match that is a period or a version (``fy2025``, ``jun2025``, ``q12026``, ``v12``) is
    not a source system. Separators inside the code are dropped so ``PC-0515`` and
    ``pc0515`` agree. (A ``PCnnn`` token, when there is one, is preferred by the caller.)
    """
    stem = Path(filename or "").stem.casefold()
    matches = re.findall(pattern, stem, flags=re.IGNORECASE)
    for found in reversed(matches):
        if isinstance(found, tuple):
            found = next((part for part in found if part), "")
        code = slug(found).replace("_", "")
        if code and not has_period(found) and not _VERSION.fullmatch(code):
            return code
    return None


_VERSION = re.compile(r"(?:v|ver|version|rev|r)\d+")


def clean_source_system(value: str | None) -> str:
    return slug(value or "").replace("_", "")


def table_name(source_system: str, sheet_names: list[str], file_stem: str = "",
               years: set[int] | None = None, legacy: bool = False) -> str:
    """The bronze table for one cleaned output.

    * every sheet carries a period, or several sheets were appended -> ``ext_src_data``
    * otherwise the sheet name, minus the source-system code if it repeats it
      (a CSV's only "sheet" is its file name) -> ``ext_src_arr``

    ``years``: the years the file covers, so a year in a sheet name counts as a period
    only when it is one of them ("Plan 2000" stays a name in a 2026 file). ``legacy``:
    the name a long one had before identifiers were hashed with SHA-256.
    """
    source = clean_source_system(source_system) or "unknown"
    names = [name for name in dict.fromkeys(sheet_names) if name]
    if not names or len(names) > 1 or any(has_period(name, years) for name in names):
        part = GENERIC
    else:
        part = "_".join(_without_source(slug(names[0]).split("_"), source)) or GENERIC
    return (legacy_identifier if legacy else identifier)(f"{PREFIX}_{source}_{part}")


def _without_source(tokens: list[str], source: str) -> list[str]:
    """Drop the source code from a name's tokens, whether written ``pc0515`` or ``pc_0515``."""
    kept, index = [], 0
    while index < len(tokens):
        if tokens[index] == source:
            index += 1
        elif index + 1 < len(tokens) and tokens[index] + tokens[index + 1] == source:
            index += 2
        else:
            kept.append(tokens[index])
            index += 1
    return [token for token in kept if token]


def identifier(name: str) -> str:
    """Fit a name in Postgres's identifier limit without two long names colliding."""
    return _fit(name, hashlib.sha256)


def legacy_identifier(name: str) -> str:
    """The name ``identifier`` gave before it hashed with SHA-256 (SHA-1), so tables and
    columns made then are still recognised. Equal to ``identifier`` for short names."""
    return _fit(name, hashlib.sha1)


def _fit(name: str, digest_of) -> str:
    name = slug(name) or "t"
    if name[0].isdigit():
        name = f"t_{name}"
    if len(name) <= MAX_IDENTIFIER:
        return name
    digest = digest_of(name.encode()).hexdigest()[:8]
    return f"{name[: MAX_IDENTIFIER - 9].rstrip('_')}_{digest}"


# Columns every bronze table carries besides the file's own (see bronze_service), and the
# internal lineage columns. A file column that would take one of these names is renamed
# (file_name -> file_name_2), so CREATE TABLE never sees a duplicate.
SYSTEM_COLUMNS = ("pc_id", "file_received_date", "reporting_start_date", "reporting_end_date", "division_name",
                  "file_name", "processing_date")
# _reporting_month: each row's month (YYYY-MM) by the date column that decided the reporting
# dates, so one month of a load can be replaced.
LINEAGE_COLUMNS = ("_ingestion_id", "_source_file", "_source_sheet", "_reporting_month", "_ingested_at")
# file_date was the file received date's earlier name: still reserved, so a file column of
# that name keeps the identifier earlier loads gave it (file_date_2).
RESERVED = SYSTEM_COLUMNS + LINEAGE_COLUMNS + ("file_date",)


def column_names(names: list[str], reserved=RESERVED) -> list[str]:
    """Column identifiers for the bronze table, unique and within the length limit."""
    used: set[str] = set(reserved)
    result = []
    for index, name in enumerate(names):
        base = identifier(name) if slug(name) else f"column_{index + 1}"
        candidate, counter = base, 2
        while candidate in used:
            suffix = f"_{counter}"
            candidate = f"{base[: MAX_IDENTIFIER - len(suffix)]}{suffix}"
            counter += 1
        used.add(candidate)
        result.append(candidate)
    return result


def next_free(name: str, taken: set[str]) -> str:
    """``name``, or ``name_2``, ``name_3`` ... whichever is not taken yet."""
    if name not in taken:
        return name
    counter = 2
    while True:
        suffix = f"_{counter}"
        candidate = f"{name[: MAX_IDENTIFIER - len(suffix)]}{suffix}"
        if candidate not in taken:
            return candidate
        counter += 1

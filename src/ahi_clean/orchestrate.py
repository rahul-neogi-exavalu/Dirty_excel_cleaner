"""Workbook-level decisions: what each table is, and which tables are appended.

Roles are read off the data's shape and content and recorded for the report. No
canonical field list is consulted, and a sheet's tab name is recorded as a hint but never
allowed to decide anything.

Tables are appended only when their column headers match exactly. Everything else ships
as its own CSV -- one per table -- and tables are never joined.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from . import flags as flag_text
from . import signals, typing_utils

FACT = "FACT"
DIMENSION = "DIMENSION"
UNKNOWN = "UNKNOWN"

STACK = "STACK"

PROFILE_OVERLAP_THRESHOLD = 0.9
KEY_DISTINCT_RATIO = 0.9
DIMENSION_MAX_ROWS = 200
SOURCE_SHEET_COLUMN = "source_sheet"


@dataclass
class Output:
    """One CSV to write, with the decisions that produced it."""

    name: str
    frame: pl.DataFrame
    kind: str
    # Region labels (``Report``, ``Report_r2``) -- which tables went into this output.
    sheets: list[str] = field(default_factory=list)
    # The workbook sheet each of those tables came from, in the same order.
    sheet_names: list[str] = field(default_factory=list)
    # Every edge case per column, joined, for the metadata's type_flag (see flags.py).
    type_flags: dict[str, str] = field(default_factory=dict)
    # Set when the output is written: one job id names the CSV and its metadata file.
    job_id: str = ""
    file: str = ""
    metadata_file: str = ""


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


def _numeric_measure_columns(frame: pl.DataFrame) -> list[str]:
    """Columns of continuous numbers -- amounts, rates -- rather than identifiers.

    Distinctness alone does not separate them: premiums are as unique as ZIP codes.
    What does is that a measure carries fractional values, or else repeats. A column of
    whole numbers that never repeats is a code -- a ZIP, a centre number -- and counting
    it as a measure would make a lookup table look transactional.

    The known limit: an integer measure that happens never to repeat (a quantity column
    of all-distinct counts) reads as a code here. Rare, and it only affects role
    classification, never the row data.
    """
    measures = []
    for column in frame.columns:
        values = frame[column].drop_nulls()
        if len(values) < 2 or not frame[column].dtype.is_numeric():
            continue
        fractional = bool((values % 1 != 0).any())
        repeats = values.n_unique() / len(values) < KEY_DISTINCT_RATIO
        # A negative value settles it on its own: identifiers, codes and ZIPs are
        # never negative, so a column that goes below zero is a quantity. This is
        # what stops an adjustment table of whole, all-distinct amounts from being
        # mistaken for a lookup.
        signed = bool((values < 0).any())
        if fractional or repeats or signed:
            measures.append(column)
    return measures


def _key_columns(frame: pl.DataFrame) -> list[str]:
    """Near-unique columns that could identify a record."""
    keys = []
    for column in frame.columns:
        values = frame[column].drop_nulls()
        if len(values) < 2:
            continue
        if values.n_unique() / len(values) >= KEY_DISTINCT_RATIO:
            keys.append(column)
    return keys


def _date_columns(frame: pl.DataFrame) -> list[str]:
    return [
        column
        for column in frame.columns
        if _all_date_like(frame[column])
    ]


def _all_date_like(series: pl.Series) -> bool:
    """Whether a column holds dates, judged on a sample rather than every value."""
    from . import coerce

    values = series.drop_nulls()
    if values.is_empty():
        return False
    return all(_is_date_like(value) for value in coerce.sample(values.to_list()))


def _is_date_like(value) -> bool:
    return typing_utils.infer_type(value) in (
        typing_utils.DATE,
        typing_utils.DATETIME,
        typing_utils.DATE_STRING,
    )


def classify_table(result) -> tuple[str, dict]:
    """Decide whether a cleaned table is transactional or a lookup."""
    frame = result.frame
    evidence = {"table": result.label, "rows": len(frame)}

    if frame.is_empty():
        evidence.update(role=UNKNOWN, reason="table is empty")
        return UNKNOWN, evidence

    measures = _numeric_measure_columns(frame)
    keys = _key_columns(frame)
    dates = _date_columns(frame)
    unique_rows = frame.n_unique() == len(frame)

    evidence.update(
        measure_columns=measures,
        key_columns=keys,
        date_columns=dates,
    )

    if measures and (keys or dates):
        role = FACT
        evidence["reason"] = "has continuous measures alongside a key or a date"
    elif not measures and unique_rows and len(frame) <= DIMENSION_MAX_ROWS and keys:
        role = DIMENSION
        evidence["reason"] = "small, one unique entity per row, no continuous measures"
    else:
        role = UNKNOWN
        evidence["reason"] = "neither transactional nor entity-shaped"

    # Recorded for the report only. The content checks above are what decide.
    evidence["name_hint"] = any(
        hint in result.sheet_name.casefold()
        for hint in ("producer", "lookup", "ref", "dim", "master")
    )
    evidence["role"] = role
    return role, evidence


# --------------------------------------------------------------------------- #
# Relationships
# --------------------------------------------------------------------------- #


def profile_overlap(left: pl.DataFrame, right: pl.DataFrame) -> float:
    """Similarity of two tables' per-column type profiles.

    Used to recognise a headerless continuation sheet. It never decides an append:
    that needs the column headers themselves to match exactly.
    """
    if len(left.columns) != len(right.columns):
        return 0.0
    left_profile = tuple(signals.modal_type(left[column].to_list()) for column in left.columns)
    right_profile = tuple(signals.modal_type(right[column].to_list()) for column in right.columns)
    return signals.profile_similarity(left_profile, right_profile)


def header_key(result) -> frozenset | None:
    """What two tables must share, exactly, to be appended: their column headers.

    A 100% match on the set of names, so a sheet that merely reorders its columns still
    appends. Nothing partial counts -- not 90% of the names, and not a matching type
    profile under different labels. A table with no header row of its own -- named
    positionally, or with names adopted from a sibling -- has nothing to match, so it
    never appends.
    """
    if not result.trace.get("header", {}).get("detected"):
        return None
    return frozenset(result.frame.columns)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def adopt_sibling_headers(results) -> list[dict]:
    """Give a headerless table the column names of a sibling it continues.

    A report split across sheets often puts the header only on the first one; the
    continuation sheet is pure data. Per sheet that is genuinely headerless and the
    extractor is right to say so -- naming a data row as the header would be worse.
    The workbook is the level that can see the answer: another table with the same
    number of columns and the same per-column types, which does have a header.

    Matching is on shape and type, never on position or sheet name, so a continuation
    is recognised whether it comes before or after the sheet it belongs to.
    """
    adoptions: list[dict] = []
    donors = [
        result
        for result in results
        if result.trace.get("header", {}).get("detected") and not result.frame.is_empty()
    ]
    for result in results:
        if result.frame.is_empty() or result.trace.get("header", {}).get("detected"):
            continue
        for donor in donors:
            if len(donor.frame.columns) != len(result.frame.columns):
                continue
            if profile_overlap(donor.frame, result.frame) < PROFILE_OVERLAP_THRESHOLD:
                continue
            renamed = dict(zip(result.frame.columns, donor.frame.columns))
            result.frame.columns = list(donor.frame.columns)
            # Types and flags were recorded under the positional names; carry them over,
            # or everything keyed by column name downstream silently misses them.
            for key in ("inferred_types", "flags"):
                recorded = result.trace.get(key, {})
                result.trace[key] = {renamed.get(name, name): value for name, value in recorded.items()}
            # "Named by position" is no longer true; say where the names came from.
            positional = {flag_text.check(flag_text.NO_HEADER), flag_text.check(flag_text.BLANK_HEADER)}
            for column in result.frame.columns:
                kept = [flag for flag in result.trace.get("flags", {}).get(column, []) if flag not in positional]
                result.trace.setdefault("flags", {})[column] = kept
                flag_text.add(result.trace, column, flag_text.check(
                    f"this sheet had no header; column names borrowed from '{donor.label}', "
                    "which has the same width and column types"
                ))
            # `detected` stays False on purpose: this region genuinely has no header
            # row, it has borrowed names from one. Overloading the flag made every
            # downstream row count skip a header line that is not there.
            result.trace["header"]["adopted_from"] = donor.label
            result.trace["header"]["names_adopted"] = True
            result.trace["notes"].append(
                f"no header on this table; adopted the column names of '{donor.label}', "
                "which has the same width and the same per-column types"
            )
            adoptions.append({"table": result.label, "adopted_from": donor.label})
            break
    return adoptions


def plan_workbook(results, stem: str) -> tuple[list[Output], dict]:
    """Classify tables, append the ones with identical headers, and produce the outputs."""
    adoptions = adopt_sibling_headers(results)
    live = [result for result in results if not result.frame.is_empty()]
    # Recorded for the report. Roles no longer decide any output: nothing is joined.
    evidence = [classify_table(result)[1] for result in live]

    report: dict = {
        "table_roles": evidence,
        "relationships": [],
        "header_adoptions": adoptions,
    }

    # --- append: tables whose headers match exactly ------------------------------
    groups: dict[frozenset, list] = {}
    for result in live:
        key = header_key(result)
        if key is not None:
            groups.setdefault(key, []).append(result)
    stacks = [group for group in groups.values() if len(group) > 1]
    stacked = {result.label for group in stacks for result in group}
    shipped = len(stacks) + sum(1 for result in live if result.label not in stacked)

    outputs: list[Output] = []
    for group in stacks:
        order = list(group[0].frame.columns)
        frames = []
        for result in group:
            # Nothing in the rows identifies which sheet they came from, and periods
            # commonly reuse the same keys. Without this the stacked rows are
            # indistinguishable and read as duplicates.
            frame = result.frame.select(order).insert_column(
                0, pl.Series(SOURCE_SHEET_COLUMN, [result.sheet_name] * len(result.frame))
            )
            frames.append(frame)
        labels = [result.label for result in group]
        name = stem if shipped == 1 else f"{stem}_{_slug(labels[0])}"
        outputs.append(
            Output(name, pl.concat(frames, how="vertical_relaxed"), "stacked", labels,
                   [result.sheet_name for result in group], _flags(group))
        )
        report["relationships"].append(
            {"tables": labels, "decision": STACK, "decided_by": "identical_headers"}
        )

    # --- everything else ships standalone, one CSV per table --------------------
    for result in live:
        if result.label in stacked:
            continue
        name = stem if shipped == 1 else f"{stem}_{_slug(result.label)}"
        outputs.append(
            Output(name, result.frame, "standalone", [result.label], [result.sheet_name],
                   _flags([result]))
        )

    return outputs, report


def _flags(results) -> dict[str, str]:
    """Every flag on every column across the tables that make up one output.

    Appended sheets can decide the same column differently -- one with more values than
    the code threshold, one with fewer -- so each sheet's flags are kept, prefixed with
    the sheet's name, rather than one winning.
    """
    merged: dict[str, list[str]] = {}
    if len(results) > 1:
        merged[SOURCE_SHEET_COLUMN] = [flag_text.info(
            "added by the cleaner: the sheet each row came from, since appended sheets "
            "can repeat the same records"
        )]
    for result in results:
        for column, column_flags in result.trace.get("flags", {}).items():
            for flag in column_flags:
                text = f"{result.sheet_name}: {flag}" if len(results) > 1 else flag
                if text not in merged.setdefault(column, []):
                    merged[column].append(text)
    return {
        column: flag_text.SEPARATOR.join(column_flags)
        for column, column_flags in merged.items()
        if column_flags
    }


def _slug(text: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in text).strip("_")

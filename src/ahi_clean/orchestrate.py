"""Workbook-level decisions: what each table is, and how tables relate.

Roles and relationships are read off the data's shape and content. No canonical field
list is consulted, and a sheet's tab name is recorded as a hint but never allowed to
decide anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from . import join, signals, typing_utils

FACT = "FACT"
DIMENSION = "DIMENSION"
UNKNOWN = "UNKNOWN"

STACK = "STACK"
JOIN = "JOIN"
NONE = "NONE"

NAME_OVERLAP_THRESHOLD = 0.9
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
    sheets: list[str] = field(default_factory=list)


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


def name_overlap(left: pl.DataFrame, right: pl.DataFrame) -> float:
    """Jaccard overlap of two tables' column names."""
    left_names, right_names = set(left.columns), set(right.columns)
    if not left_names or not right_names:
        return 0.0
    return len(left_names & right_names) / len(left_names | right_names)


def profile_overlap(left: pl.DataFrame, right: pl.DataFrame) -> float:
    """Similarity of two tables' per-column type profiles.

    The fallback for sheets that hold the same data under different labels -- a
    January export headed differently from February's.
    """
    if len(left.columns) != len(right.columns):
        return 0.0
    left_profile = tuple(signals.modal_type(left[column].to_list()) for column in left.columns)
    right_profile = tuple(signals.modal_type(right[column].to_list()) for column in right.columns)
    return signals.profile_similarity(left_profile, right_profile)


def _stack_decision(left, right) -> tuple[bool, dict]:
    names = name_overlap(left.frame, right.frame)
    profile = profile_overlap(left.frame, right.frame)
    by_name = names >= NAME_OVERLAP_THRESHOLD
    by_profile = profile >= PROFILE_OVERLAP_THRESHOLD and len(left.frame.columns) == len(
        right.frame.columns
    )
    detail = {
        "tables": [left.label, right.label],
        "name_overlap": round(names, 3),
        "profile_overlap": round(profile, 3),
        "decision": STACK if (by_name or by_profile) else NONE,
        "decided_by": "column_names" if by_name else ("type_profile" if by_profile else None),
    }
    if by_profile and not by_name:
        detail["confidence"] = "low"
    return by_name or by_profile, detail


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
            result.frame.columns = list(donor.frame.columns)
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
    """Classify tables, decide their relationships, and produce the outputs."""
    adoptions = adopt_sibling_headers(results)
    live = [result for result in results if not result.frame.is_empty()]
    roles: dict[str, str] = {}
    evidence: list[dict] = []
    for result in live:
        role, why = classify_table(result)
        roles[result.label] = role
        evidence.append(why)

    report: dict = {
        "table_roles": evidence,
        "relationships": [],
        "join": None,
        "header_adoptions": adoptions,
    }
    facts = [result for result in live if roles[result.label] == FACT]
    dimensions = [result for result in live if roles[result.label] == DIMENSION]

    # --- stack: several fact tables with the same shape -------------------------
    stack_group: list = []
    if len(facts) > 1:
        decisions = []
        for index in range(len(facts) - 1):
            stackable, detail = _stack_decision(facts[index], facts[index + 1])
            decisions.append(stackable)
            report["relationships"].append(detail)
        if all(decisions):
            stack_group = facts

    outputs: list[Output] = []
    if stack_group:
        frames = []
        for result in stack_group:
            # Nothing in the rows identifies which sheet they came from, and periods
            # commonly reuse the same keys. Without this the stacked rows are
            # indistinguishable and read as duplicates.
            frame = result.frame.insert_column(
                0, pl.Series(SOURCE_SHEET_COLUMN, [result.sheet_name] * len(result.frame))
            )
            frames.append(frame)
        outputs.append(
            Output(stem, pl.concat(frames, how="vertical_relaxed"), "stacked",
                   [result.label for result in stack_group])
        )
        remaining_facts = []
    else:
        remaining_facts = facts

    # --- join: one fact table enriched by one lookup ----------------------------
    if len(remaining_facts) == 1 and len(dimensions) == 1:
        fact, dimension = remaining_facts[0], dimensions[0]
        key = join.discover_key(fact.frame, dimension.frame)
        report["relationships"].append(
            {
                "tables": [fact.label, dimension.label],
                "roles": [FACT, DIMENSION],
                "name_overlap": round(name_overlap(fact.frame, dimension.frame), 3),
                "decision": JOIN if key else NONE,
                "discovered_key": key,
            }
        )
        if key:
            merged, join_report = join.join_frames(fact.frame, dimension.frame, key)
            report["join"] = join_report
            outputs.append(Output(stem, merged, "joined", [fact.label, dimension.label]))
            # The lookup still ships separately: it is reference data in its own right.
            outputs.append(
                Output(f"{stem}_{_slug(dimension.label)}", dimension.frame, "dimension",
                       [dimension.label])
            )

    # --- everything else ships standalone ---------------------------------------
    emitted = {sheet for output in outputs for sheet in output.sheets}
    for result in live:
        if result.label in emitted:
            continue
        name = stem if len(live) == 1 else f"{stem}_{_slug(result.label)}"
        outputs.append(Output(name, result.frame, "standalone", [result.label]))

    return outputs, report


def _slug(text: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in text).strip("_")

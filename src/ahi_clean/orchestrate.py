"""Workbook-level decisions: what each sheet is, and how sheets relate.

Sheet roles and relationships are inferred from column content. Sheet names are a
supporting signal only -- a sheet called "Producer" that turns out to hold
transactional rows is a fact table regardless of its tab label.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import join, schema
from .extract import SheetResult

FACT = "FACT"
DIMENSION = "DIMENSION"
UNKNOWN = "UNKNOWN"

STACK = "STACK"
JOIN = "JOIN"
NONE = "NONE"

STACK_OVERLAP_THRESHOLD = 0.9
DIMENSION_MAX_ROWS = 200
SOURCE_SHEET_COLUMN = "source_sheet"


@dataclass
class Output:
    """One CSV to write, with the decisions that produced it."""

    name: str
    frame: pd.DataFrame
    kind: str
    sheets: list[str] = field(default_factory=list)


def classify_sheet(result: SheetResult) -> tuple[str, dict]:
    """Decide whether a cleaned sheet is a fact table or a lookup."""
    frame = result.frame
    evidence = {
        "sheet": result.sheet_name,
        "rows": len(frame),
        "fact_signal_fields": sorted(set(frame.columns) & schema.FACT_SIGNAL_FIELDS),
    }

    if frame.empty:
        evidence["reason"] = "sheet is empty"
        return UNKNOWN, evidence

    has_fact_signals = len(evidence["fact_signal_fields"]) >= 2
    rows_are_unique_entities = len(frame.drop_duplicates()) == len(frame)
    small = len(frame) <= DIMENSION_MAX_ROWS

    if has_fact_signals:
        evidence["reason"] = "carries transactional fields (premium / policy / date)"
        role = FACT
    elif small and rows_are_unique_entities:
        evidence["reason"] = "small, one unique entity per row, no transactional fields"
        role = DIMENSION
    else:
        evidence["reason"] = "no transactional fields and not entity-shaped"
        role = UNKNOWN

    # Recorded for the audit trail, never used to override the content check above.
    evidence["name_hint"] = any(
        hint in result.sheet_name.casefold() for hint in ("producer", "lookup", "ref", "dim", "master")
    )
    evidence["role"] = role
    return role, evidence


def schema_overlap(left: pd.DataFrame, right: pd.DataFrame) -> float:
    """Jaccard overlap of the canonical fields present on two sheets."""
    left_fields = set(left.columns) & set(schema.CANONICAL_FIELDS)
    right_fields = set(right.columns) & set(schema.CANONICAL_FIELDS)
    if not left_fields or not right_fields:
        return 0.0
    return len(left_fields & right_fields) / len(left_fields | right_fields)


def plan_workbook(results: list[SheetResult], stem: str) -> tuple[list[Output], dict]:
    """Classify sheets, decide their relationships, and produce the outputs."""
    roles: dict[str, str] = {}
    evidence: list[dict] = []
    for result in results:
        role, why = classify_sheet(result)
        roles[result.sheet_name] = role
        evidence.append(why)

    report: dict = {"sheet_roles": evidence, "relationships": [], "join": None}
    by_name = {result.sheet_name: result for result in results}

    facts = [result for result in results if roles[result.sheet_name] == FACT and not result.frame.empty]
    dimensions = [
        result for result in results if roles[result.sheet_name] == DIMENSION and not result.frame.empty
    ]

    # --- stack: several fact sheets sharing one schema (File5's Jan / Feb) ---------
    stack_group: list[SheetResult] = []
    if len(facts) > 1:
        overlaps = []
        for index in range(len(facts) - 1):
            overlap = schema_overlap(facts[index].frame, facts[index + 1].frame)
            overlaps.append(overlap)
            report["relationships"].append(
                {
                    "sheets": [facts[index].sheet_name, facts[index + 1].sheet_name],
                    "schema_overlap": round(overlap, 3),
                    "decision": STACK if overlap >= STACK_OVERLAP_THRESHOLD else NONE,
                }
            )
        if overlaps and min(overlaps) >= STACK_OVERLAP_THRESHOLD:
            stack_group = facts

    outputs: list[Output] = []

    if stack_group:
        frames = []
        for result in stack_group:
            frame = result.frame.copy()
            # Nothing in the rows themselves says which period they belong to, and
            # Jan and Feb reuse the same policy numbers. Without this column the
            # stacked rows are indistinguishable and read as duplicates.
            frame.insert(0, SOURCE_SHEET_COLUMN, result.sheet_name)
            frames.append(frame)
        stacked = pd.concat(frames, ignore_index=True)
        outputs.append(
            Output(stem, stacked, "stacked", [result.sheet_name for result in stack_group])
        )
        remaining_facts = []
    else:
        remaining_facts = facts

    # --- join: one fact sheet enriched by a lookup sheet (File6) -------------------
    joined_facts: set[str] = set()
    if len(remaining_facts) == 1 and len(dimensions) == 1:
        fact_result, dimension_result = remaining_facts[0], dimensions[0]
        overlap = schema_overlap(fact_result.frame, dimension_result.frame)
        key = join.discover_key(fact_result.frame, dimension_result.frame)
        decision = JOIN if key else NONE
        report["relationships"].append(
            {
                "sheets": [fact_result.sheet_name, dimension_result.sheet_name],
                "schema_overlap": round(overlap, 3),
                "roles": [FACT, DIMENSION],
                "decision": decision,
                "discovered_key": key,
            }
        )
        if key:
            merged, join_report = join.join_frames(fact_result.frame, dimension_result.frame, key)
            report["join"] = join_report
            outputs.append(
                Output(stem, merged, "joined", [fact_result.sheet_name, dimension_result.sheet_name])
            )
            joined_facts.add(fact_result.sheet_name)
            # The lookup still ships on its own: it is reference data in its own right.
            outputs.append(
                Output(f"{stem}_{_slug(dimension_result.sheet_name)}", dimension_result.frame, "dimension",
                       [dimension_result.sheet_name])
            )

    # --- anything left over ships standalone (Files 1-4) ---------------------------
    emitted = {sheet for output in outputs for sheet in output.sheets}
    for result in results:
        if result.sheet_name in emitted or result.frame.empty:
            continue
        name = stem if len(results) == 1 else f"{stem}_{_slug(result.sheet_name)}"
        outputs.append(Output(name, result.frame, "standalone", [result.sheet_name]))

    return outputs, report


def _slug(text: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in text).strip("_")

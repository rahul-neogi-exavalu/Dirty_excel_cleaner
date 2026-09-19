"""Workbook behaviour: roles, stacking, joining, the CLI, and the audit report.

Built in memory, so swapping the sample corpus cannot break what these pin.
"""

import json

import pandas as pd
import pytest

from ahi_clean import orchestrate
from ahi_clean.cli import clean_workbook
from ahi_clean.orchestrate import DIMENSION, FACT, plan_workbook

from conftest import HEADER, SAMPLES, blanks, one, records, scenario_files, sheet

LOOKUP_HEADER = ["Producer", "Address", "State", "ZIP Code"]
LOOKUP_ROWS = [
    ["Pinnacle Agency Partners", "9515 Delegates Row", "IN", "46240"],
    ["Apex Insurance Brokers", "20 Village Center Dr", "NJ", "08085"],
    ["Metro Agency Group", "32646 Five Mile Rd", "MI", "48154"],
    ["Coastal Risk Advisors", "1514 Roberts Dr", "FL", "32250"],
]


def fact_table(count=8):
    return one([HEADER] + records(count))


def lookup_table():
    return one([LOOKUP_HEADER] + LOOKUP_ROWS, name="Lookup")


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #


def test_a_transactional_table_is_a_fact():
    role, why = orchestrate.classify_table(fact_table())
    assert role == FACT
    assert "premium" in why["measure_columns"]


def test_an_entity_lookup_is_a_dimension():
    role, why = orchestrate.classify_table(lookup_table())
    assert role == DIMENSION
    # A ZIP is a whole number that never repeats -- a code, not a measure.
    assert why["measure_columns"] == []


def test_the_sheet_name_is_a_hint_and_never_the_deciding_vote():
    result = fact_table()
    result.sheet_name = "Producer Lookup"
    role, why = orchestrate.classify_table(result)
    assert role == FACT
    assert why["name_hint"] is True


def test_a_table_of_whole_distinct_amounts_is_a_fact_when_any_is_negative():
    """Identifiers and ZIPs are never negative, so a negative settles it."""
    rows = [["Policy Number", "Premium", "Adjustment"]] + [
        [f"POL-{index}", 100 + index, 25 if index % 2 else -10] for index in range(5)
    ]
    role, why = orchestrate.classify_table(one(rows))
    assert role == FACT
    assert why["measure_columns"]


# --------------------------------------------------------------------------- #
# Joins
# --------------------------------------------------------------------------- #


def test_a_fact_is_joined_to_its_lookup_and_the_lookup_also_ships():
    outputs, report = plan_workbook([fact_table(), lookup_table()], "book")
    kinds = {output.kind: output for output in outputs}

    assert "joined" in kinds and "dimension" in kinds
    assert {"address", "state", "zip_code"} <= set(kinds["joined"].frame.columns)
    # The two columns are named differently in their own files; the pair is found from
    # the values, never from the labels.
    assert report["join"]["join_key"]["fact_column"] == "producer_agencyname"
    assert report["join"]["join_key"]["dimension_column"] == "producer"


def test_a_lookup_is_recognised_even_when_it_comes_first():
    """Position on the sheet says nothing about what a table is."""
    outputs, _report = plan_workbook([lookup_table(), fact_table()], "book")
    assert {output.kind for output in outputs} == {"joined", "dimension"}


def test_a_join_never_loses_a_fact_row():
    fact = fact_table()
    outputs, _report = plan_workbook([fact, lookup_table()], "book")
    joined = [output for output in outputs if output.kind == "joined"][0]
    assert len(joined.frame) == len(fact.frame)


def test_unresolved_lookup_values_are_reported_not_silently_merged():
    """A producer the lookup has never heard of keeps its row and is flagged."""
    rows = [HEADER] + records(8)
    rows[1][2] = "Brown & Brown"  # absent from LOOKUP_ROWS
    fact = one(rows)
    outputs, report = plan_workbook([fact, lookup_table()], "book")

    needs_review = report["join"]["needs_review"]
    assert any(entry["source_value"] == "Brown & Brown" for entry in needs_review)
    assert all(entry["matched_value"] is None for entry in needs_review)
    joined = [output for output in outputs if output.kind == "joined"][0]
    assert len(joined.frame) == len(fact.frame)


# --------------------------------------------------------------------------- #
# Continuation sheets
# --------------------------------------------------------------------------- #


def test_a_headerless_continuation_adopts_its_siblings_column_names():
    """A report split over sheets often carries the header only on the first."""
    first = one([HEADER] + records(10), name="Report")
    second = one(records(10, start=10), name="Continued")
    assert second.trace["header"]["detected"] is False

    plan_workbook([first, second], "book")
    assert list(second.frame.columns) == list(first.frame.columns)
    assert second.trace["header"]["adopted_from"] == first.label


def test_a_continuation_is_matched_on_shape_not_on_sheet_order():
    first = one(records(10), name="Continued")
    second = one([HEADER] + records(10, start=10), name="Report")
    plan_workbook([first, second], "book")
    assert list(first.frame.columns) == list(second.frame.columns)


def test_a_headerless_table_of_a_different_shape_does_not_adopt():
    """Adoption needs the same width and the same per-column types, or it is a guess."""
    fact = fact_table()
    narrow = one(
        [[f"Zone {index} PC", 1000 + index, f"note {index}"] for index in range(8)],
        name="Notes",
    )
    before = list(narrow.frame.columns)
    plan_workbook([fact, narrow], "book")
    assert list(narrow.frame.columns) == before
    assert "profitcentername" not in narrow.frame.columns


# --------------------------------------------------------------------------- #
# Stacking
# --------------------------------------------------------------------------- #


def test_matching_tables_are_stacked_with_provenance():
    january = one([HEADER] + records(6), name="Jan")
    february = one([HEADER] + records(6), name="Feb")
    [output] = plan_workbook([january, february], "book")[0]

    assert output.kind == "stacked"
    assert len(output.frame) == 12
    assert output.frame[orchestrate.SOURCE_SHEET_COLUMN].tolist() == ["Jan"] * 6 + ["Feb"] * 6


def test_stacked_periods_are_not_deduplicated():
    """Periods legitimately reuse keys; both must survive."""
    [output] = plan_workbook(
        [one([HEADER] + records(6), name="Jan"), one([HEADER] + records(6), name="Feb")], "book"
    )[0]
    assert output.frame["policynumber"].nunique() == 6
    assert len(output.frame) == 12


def test_tables_with_the_same_shape_but_different_labels_stack_at_low_confidence():
    left = one([HEADER] + records(6), name="Jan")
    right = one([HEADER] + records(6), name="Feb")
    right.frame = right.frame.rename(columns={name: f"{name}_b" for name in right.frame.columns})
    stackable, detail = orchestrate._stack_decision(left, right)
    assert stackable is True
    assert detail["decided_by"] == "type_profile"
    assert detail["confidence"] == "low"


def test_unrelated_tables_are_not_stacked():
    left, right = fact_table().frame, lookup_table().frame
    assert orchestrate.name_overlap(left, right) < orchestrate.NAME_OVERLAP_THRESHOLD
    assert orchestrate.profile_overlap(left, right) < orchestrate.PROFILE_OVERLAP_THRESHOLD


def test_a_single_table_workbook_ships_standalone():
    [output] = plan_workbook([fact_table()], "book")[0]
    assert output.kind == "standalone"
    assert orchestrate.SOURCE_SHEET_COLUMN not in output.frame.columns


# --------------------------------------------------------------------------- #
# CLI and audit, over whatever corpus is present
# --------------------------------------------------------------------------- #


def _a_scenario_file():
    paths = scenario_files()
    if not paths:
        pytest.skip("no scenario workbooks in the corpus")
    return paths[0]


def test_the_cli_writes_csvs_and_an_audit_report(tmp_path):
    outcome = clean_workbook(_a_scenario_file(), tmp_path / "cleaned", tmp_path / "audit")
    assert outcome["written"]
    assert outcome["report"].exists()
    for path in outcome["written"]:
        assert len(pd.read_csv(path)) >= 0


def test_a_locked_output_file_does_not_abort_the_run(tmp_path, monkeypatch):
    """Excel holds a lock on an open CSV; the rest of the work must still land."""
    original = pd.DataFrame.to_csv
    calls = {"n": 0}

    def flaky(self, path=None, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked by another program")
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_csv", flaky)
    outcome = clean_workbook(_a_scenario_file(), tmp_path / "cleaned", tmp_path / "audit")
    assert outcome["blocked"]
    assert outcome["report"].exists()


def test_the_audit_report_records_structure_and_reasons(tmp_path):
    outcome = clean_workbook(_a_scenario_file(), tmp_path / "cleaned", tmp_path / "audit")
    report = json.loads(outcome["report"].read_text(encoding="utf-8"))

    assert report["summary"]["tables_found"] >= 1
    for table in report["tables"]:
        assert "orientation" in table
        assert all(row["reason"] for row in table.get("dropped_rows", []))

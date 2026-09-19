"""Workbook-level behaviour: sheet roles, stacking, joining, and the CLI."""

import json

import pandas as pd
import pytest

from ahi_clean import orchestrate
from ahi_clean.cli import clean_workbook
from ahi_clean.orchestrate import DIMENSION, FACT, plan_workbook

from conftest import SAMPLES


def test_sheet_roles_are_decided_on_content(file6):
    fact_role, fact_why = orchestrate.classify_sheet(file6[0])
    dimension_role, dimension_why = orchestrate.classify_sheet(file6[1])

    assert fact_role == FACT
    assert set(fact_why["fact_signal_fields"]) >= {"premium", "policy_number"}
    assert dimension_role == DIMENSION
    assert dimension_why["fact_signal_fields"] == []


def test_a_lookup_named_sheet_holding_transactions_is_still_a_fact(file6):
    """The tab label is a hint in the report, never the deciding vote."""
    disguised = file6[0]
    disguised.sheet_name = "Producer Lookup"
    role, why = orchestrate.classify_sheet(disguised)
    assert role == FACT
    assert why["name_hint"] is True


def test_matching_sheets_are_stacked_with_provenance(file5):
    outputs, report = plan_workbook(file5, "File5")
    [output] = outputs

    assert output.kind == "stacked"
    assert len(output.frame) == 20
    assert output.frame[orchestrate.SOURCE_SHEET_COLUMN].tolist() == ["Jan"] * 10 + ["Feb"] * 10
    assert report["relationships"][0]["schema_overlap"] == 1.0
    assert report["relationships"][0]["decision"] == "STACK"


def test_stacked_periods_are_not_deduplicated(file5):
    """Jan and Feb reuse the same policy numbers; both periods must survive."""
    [output] = plan_workbook(file5, "File5")[0]
    assert output.frame["policy_number"].nunique() == 10
    assert len(output.frame) == 20
    grouped = output.frame.groupby(orchestrate.SOURCE_SHEET_COLUMN)["policy_number"].nunique()
    assert grouped.to_dict() == {"Jan": 10, "Feb": 10}


def test_fact_and_dimension_are_joined_and_the_lookup_also_ships(file6):
    outputs, report = plan_workbook(file6, "File6")
    kinds = {output.kind: output for output in outputs}

    assert set(kinds) == {"joined", "dimension"}
    assert len(kinds["joined"].frame) == 10
    assert {"address", "state", "zip_code"} <= set(kinds["joined"].frame.columns)
    assert len(kinds["dimension"].frame) == 5
    assert report["join"]["join_key"]["fact_column"] == "producer_agency_name"


def test_single_sheet_workbooks_ship_standalone(file1, file3):
    for results, stem in ((file1, "File1"), (file3, "File3")):
        [output] = plan_workbook(results, stem)[0]
        assert output.kind == "standalone"
        assert output.name == stem
        assert orchestrate.SOURCE_SHEET_COLUMN not in output.frame.columns


def test_unrelated_sheets_are_not_stacked(file1, file6):
    """Different schemas must not be concatenated just because both are facts."""
    assert orchestrate.schema_overlap(file1[0].frame, file6[1].frame) < orchestrate.STACK_OVERLAP_THRESHOLD


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("File1_Scenarios1-4_Combined.xlsx", {"File1_Scenarios1-4_Combined.csv": 10}),
        ("File2_Scenarios5-8_Combined.xlsx", {"File2_Scenarios5-8_Combined.csv": 10}),
        ("File3_Scenario_subtotals_in_middle.xlsx", {"File3_Scenario_subtotals_in_middle.csv": 10}),
        ("File4_Scenario_transposed.xlsx", {"File4_Scenario_transposed.csv": 10}),
        ("File5_Scenario_MultiSheet.xlsx", {"File5_Scenario_MultiSheet.csv": 20}),
        (
            "File6_Scenarios_MultipleSheetJoin.xlsx",
            {
                "File6_Scenarios_MultipleSheetJoin.csv": 10,
                "File6_Scenarios_MultipleSheetJoin_Producer.csv": 5,
            },
        ),
    ],
)
def test_end_to_end_row_counts(filename, expected, tmp_path):
    outcome = clean_workbook(SAMPLES / filename, tmp_path / "cleaned", tmp_path / "audit")
    written = {path.name: len(pd.read_csv(path)) for path in outcome["written"]}
    assert written == expected


def test_audit_report_explains_what_was_dropped(tmp_path):
    outcome = clean_workbook(
        SAMPLES / "File2_Scenarios5-8_Combined.xlsx", tmp_path / "cleaned", tmp_path / "audit"
    )
    report = json.loads(outcome["report"].read_text(encoding="utf-8"))

    assert report["embedded_images"] == 1
    assert report["summary"]["dropped_by_classification"] == {"BANNER": 3, "FOOTER": 2, "SUBTOTAL": 1}
    assert report["sheets"][0]["error_cells_nulled"] == ["A1=#VALUE!"]


def test_audit_report_records_the_fuzzy_join(tmp_path):
    outcome = clean_workbook(
        SAMPLES / "File6_Scenarios_MultipleSheetJoin.xlsx", tmp_path / "cleaned", tmp_path / "audit"
    )
    report = json.loads(outcome["report"].read_text(encoding="utf-8"))

    resolution = next(
        entry for entry in report["workbook"]["join"]["resolutions"] if entry["source_value"] == "MJC"
    )
    assert resolution["matched_value"] == "MJC Agency Group"
    assert resolution["best_score"] == 100.0
    assert resolution["runner_up_score"] < 90
    assert report["summary"]["join_values_needing_review"] == 0


def test_cleaned_csv_has_no_nulls_in_the_record_key(tmp_path):
    for filename in sorted(path.name for path in SAMPLES.glob("*.xlsx")):
        outcome = clean_workbook(SAMPLES / filename, tmp_path / "cleaned", tmp_path / "audit")
        for path in outcome["written"]:
            frame = pd.read_csv(path)
            if "policy_number" in frame.columns:
                assert frame["policy_number"].notna().all(), path.name
                assert frame["premium"].dtype.kind == "f", path.name

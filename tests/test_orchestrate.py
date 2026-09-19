"""Workbook behaviour: roles, stacking, joining, the CLI, and the audit report."""

import json

import pandas as pd
import pytest

from ahi_clean import orchestrate
from ahi_clean.cli import clean_workbook
from ahi_clean.orchestrate import DIMENSION, FACT, plan_workbook

from conftest import SAMPLES, tables


def test_roles_are_decided_on_column_content(file6):
    fact_role, fact_why = orchestrate.classify_table(file6[0][0])
    dimension_role, dimension_why = orchestrate.classify_table(file6[1][0])

    assert fact_role == FACT
    assert "premium" in fact_why["measure_columns"]
    assert dimension_role == DIMENSION
    # A ZIP code is a whole number that never repeats -- a code, not a measure.
    assert dimension_why["measure_columns"] == []


def test_a_lookup_named_sheet_holding_transactions_is_still_a_fact(file6):
    """The tab label is a hint in the report, never the deciding vote."""
    disguised = file6[0][0]
    disguised.sheet_name = "Producer Lookup"
    role, why = orchestrate.classify_table(disguised)
    assert role == FACT
    assert why["name_hint"] is True


def test_matching_sheets_are_stacked_with_provenance(file5):
    outputs, report = plan_workbook(tables("File5_Scenario_MultiSheet.xlsx"), "File5")
    [output] = outputs

    assert output.kind == "stacked"
    assert len(output.frame) == 20
    assert output.frame[orchestrate.SOURCE_SHEET_COLUMN].tolist() == ["Jan"] * 10 + ["Feb"] * 10
    assert report["relationships"][0]["name_overlap"] == 1.0
    assert report["relationships"][0]["decided_by"] == "column_names"


def test_stacked_periods_are_not_deduplicated():
    """Jan and Feb reuse the same policy numbers; both periods must survive."""
    [output] = plan_workbook(tables("File5_Scenario_MultiSheet.xlsx"), "File5")[0]
    assert output.frame["policynumber"].nunique() == 10
    assert len(output.frame) == 20
    grouped = output.frame.groupby(orchestrate.SOURCE_SHEET_COLUMN)["policynumber"].nunique()
    assert grouped.to_dict() == {"Jan": 10, "Feb": 10}


def test_sheets_can_stack_on_type_profile_when_headers_differ():
    """A January export headed differently from February still stacks, flagged low."""
    january = tables("File5_Scenario_MultiSheet.xlsx")[0]
    february = tables("File5_Scenario_MultiSheet.xlsx")[1]
    february.frame = february.frame.rename(
        columns={name: f"{name}_feb" for name in february.frame.columns}
    )
    stackable, detail = orchestrate._stack_decision(january, february)
    assert stackable is True
    assert detail["decided_by"] == "type_profile"
    assert detail["confidence"] == "low"


def test_fact_and_dimension_are_joined_and_the_lookup_also_ships(file6):
    outputs, report = plan_workbook(tables("File6_Scenarios_MultipleSheetJoin.xlsx"), "File6")
    kinds = {output.kind: output for output in outputs}

    assert set(kinds) == {"joined", "dimension"}
    assert len(kinds["joined"].frame) == 10
    assert {"address", "state", "zip_code"} <= set(kinds["joined"].frame.columns)
    assert len(kinds["dimension"].frame) == 5
    assert report["join"]["join_key"]["fact_column"] == "producer_agencyname"


def test_single_sheet_workbooks_ship_standalone():
    for filename, stem in (
        ("File1_Scenarios1-4_Combined.xlsx", "File1"),
        ("File3_Scenario_subtotals_in_middle.xlsx", "File3"),
    ):
        [output] = plan_workbook(tables(filename), stem)[0]
        assert output.kind == "standalone"
        assert output.name == stem
        assert orchestrate.SOURCE_SHEET_COLUMN not in output.frame.columns


def test_unrelated_sheets_are_not_stacked():
    """Different schemas must not be concatenated just because both look tabular."""
    left = tables("File1_Scenarios1-4_Combined.xlsx")[0].frame
    right = tables("File6_Scenarios_MultipleSheetJoin.xlsx")[1].frame
    assert orchestrate.name_overlap(left, right) < orchestrate.NAME_OVERLAP_THRESHOLD
    assert orchestrate.profile_overlap(left, right) < orchestrate.PROFILE_OVERLAP_THRESHOLD


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


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
    assert report["summary"]["dropped_by_classification"] == {
        "BANNER": 2,
        "FOOTER": 2,
        "GRAND_TOTAL": 1,
    }
    assert report["tables"][0]["error_cells_nulled"] == ["A1=#VALUE!"]


def test_audit_report_gives_a_reason_for_every_dropped_row(tmp_path):
    for filename in sorted(path.name for path in SAMPLES.glob("*.xlsx")):
        outcome = clean_workbook(SAMPLES / filename, tmp_path / "cleaned", tmp_path / "audit")
        report = json.loads(outcome["report"].read_text(encoding="utf-8"))
        for table in report["tables"]:
            for dropped in table["dropped_rows"]:
                assert dropped["reason"], f"{filename}: {dropped}"
                assert dropped["sheet_row"] is not None


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


def test_audit_report_flags_low_confidence_structure(tmp_path):
    """The Producer lookup is near-square, so its orientation is reported as uncertain."""
    outcome = clean_workbook(
        SAMPLES / "File6_Scenarios_MultipleSheetJoin.xlsx", tmp_path / "cleaned", tmp_path / "audit"
    )
    report = json.loads(outcome["report"].read_text(encoding="utf-8"))
    assert report["summary"]["low_confidence_orientations"] == 1
    assert report["summary"]["headerless_tables"] == 0


def test_no_cleaned_csv_loses_its_key_column(tmp_path):
    for filename in sorted(path.name for path in SAMPLES.glob("*.xlsx")):
        outcome = clean_workbook(SAMPLES / filename, tmp_path / "cleaned", tmp_path / "audit")
        for path in outcome["written"]:
            frame = pd.read_csv(path)
            if "policynumber" in frame.columns:
                assert frame["policynumber"].notna().all(), path.name
                assert frame["premium"].dtype.kind == "f", path.name

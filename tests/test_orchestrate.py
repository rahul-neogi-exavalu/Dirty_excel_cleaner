"""Workbook behaviour: roles, appending, the CLI, and the audit report.

Built in memory, so swapping the sample corpus cannot break what these pin.
"""

import json

import polars as pl
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
# No joins: one CSV per table
# --------------------------------------------------------------------------- #


def test_a_fact_and_a_lookup_ship_as_separate_csvs_and_are_never_joined():
    fact, lookup = fact_table(), lookup_table()
    outputs, report = plan_workbook([fact, lookup], "book")

    assert [output.kind for output in outputs] == ["standalone", "standalone"]
    assert len({output.name for output in outputs}) == 2
    by_sheet = {output.sheets[0]: output for output in outputs}
    assert list(by_sheet[fact.label].frame.columns) == list(fact.frame.columns)
    assert list(by_sheet[lookup.label].frame.columns) == list(lookup.frame.columns)
    assert "join" not in report


def test_n_differently_headed_sheets_give_n_csvs():
    codes = one([["Code", "Label", "Rank"]] + [[f"C{i}", f"label {i}", i] for i in range(5)], name="Codes")
    outputs, _report = plan_workbook([fact_table(), lookup_table(), codes], "book")
    assert len(outputs) == 3
    assert all(output.kind == "standalone" for output in outputs)


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
    assert output.frame[orchestrate.SOURCE_SHEET_COLUMN].to_list() == ["Jan"] * 6 + ["Feb"] * 6


def test_stacked_periods_are_not_deduplicated():
    """Periods legitimately reuse keys; both must survive."""
    [output] = plan_workbook(
        [one([HEADER] + records(6), name="Jan"), one([HEADER] + records(6), name="Feb")], "book"
    )[0]
    assert output.frame["policynumber"].n_unique() == 6
    assert len(output.frame) == 12


def test_tables_with_the_same_shape_but_different_labels_are_not_appended():
    """A matching type profile is not a header match."""
    left = one([HEADER] + records(6), name="Jan")
    right = one([HEADER] + records(6), name="Feb")
    right.frame = right.frame.rename({name: f"{name}_b" for name in right.frame.columns})
    outputs, _report = plan_workbook([left, right], "book")
    assert [output.kind for output in outputs] == ["standalone", "standalone"]


def test_a_partial_header_match_is_not_appended():
    """Seven of eight names matching is not enough: only 100% appends."""
    left = one([HEADER] + records(6), name="Jan")
    right = one([HEADER] + records(6), name="Feb")
    right.frame = right.frame.rename({"commission": "commission_rate"})
    outputs, _report = plan_workbook([left, right], "book")
    assert [output.kind for output in outputs] == ["standalone", "standalone"]


def test_reordered_columns_with_identical_headers_are_appended_in_the_first_order():
    left = one([HEADER] + records(6), name="Jan")
    right = one([HEADER] + records(6), name="Feb")
    right.frame = right.frame.select(list(reversed(right.frame.columns)))
    [output] = plan_workbook([left, right], "book")[0]
    assert output.kind == "stacked"
    assert list(output.frame.columns) == [orchestrate.SOURCE_SHEET_COLUMN] + list(left.frame.columns)
    assert len(output.frame) == 12


def test_only_the_matching_sheets_are_appended():
    january = one([HEADER] + records(6), name="Jan")
    february = one([HEADER] + records(6), name="Feb")
    outputs, _report = plan_workbook([january, lookup_table(), february], "book")
    assert sorted(output.kind for output in outputs) == ["stacked", "standalone"]
    stacked = [output for output in outputs if output.kind == "stacked"][0]
    assert stacked.sheets == [january.label, february.label]


def test_a_continuation_with_adopted_names_is_not_appended():
    """Borrowed names are not a header match; the continuation ships on its own."""
    first = one([HEADER] + records(10), name="Report")
    second = one(records(10, start=10), name="Continued")
    outputs, _report = plan_workbook([first, second], "book")
    assert [output.kind for output in outputs] == ["standalone", "standalone"]
    assert list(second.frame.columns) == list(first.frame.columns)


def test_unrelated_tables_are_not_stacked():
    outputs, _report = plan_workbook([fact_table(), lookup_table()], "book")
    assert "stacked" not in {output.kind for output in outputs}


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
        assert len(pl.read_csv(path)) >= 0


def test_a_locked_output_file_does_not_abort_the_run(tmp_path, monkeypatch):
    """Excel holds a lock on an open CSV; the rest of the work must still land."""
    original = pl.DataFrame.write_csv
    calls = {"n": 0}

    def flaky(self, path=None, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # How polars reports a Windows sharing violation -- a bare OSError, not
            # the PermissionError pandas used to raise.
            raise OSError(32, "The process cannot access the file because it is being "
                              "used by another process.")
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_csv", flaky)
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

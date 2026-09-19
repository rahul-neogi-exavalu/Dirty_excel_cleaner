"""Structural heuristics: orientation, header hunt, row classification, dtypes."""

import pandas as pd
import pytest

from ahi_clean import extract, schema
from ahi_clean.extract import extract_sheet
from ahi_clean.reader import SheetGrid

CANONICAL_HEADER = [
    "profitCenterName",
    "ProfitCenterNumber",
    "Producer/AgencyName",
    "InsuranceCompanyName",
    "Premium",
    "PolicyNumber",
    "AccountingEffectiveDate",
    "Commission%",
]


def test_every_sample_sheet_yields_the_canonical_schema(file1, file2, file3, file4, file5, file6):
    for result in file1 + file2 + file3 + file4 + file5 + file6[:1]:
        assert set(result.frame.columns) == set(
            name for name in schema.CANONICAL_FIELDS if name not in {"address", "state", "zip_code"}
        )
        assert len(result.frame) == 10


def test_transposed_sheet_is_detected_and_flipped(file4):
    orientation = file4[0].trace["orientation"]
    assert orientation["orientation"] == "transposed"
    # The primary vote must decide this outright, not fall through to the tiebreaker.
    assert orientation["decided_by"] == "alias_match"
    assert orientation["column_alias_rate"] > orientation["row_alias_rate"]


@pytest.mark.parametrize("fixture_name", ["file1", "file2", "file3", "file5", "file6"])
def test_upright_sheets_are_not_transposed(fixture_name, request):
    for result in request.getfixturevalue(fixture_name):
        assert result.trace["orientation"]["orientation"] == "normal"


def test_header_is_found_below_a_banner(file2):
    # The block starts at the 'Exavalu' title row (sheet row 3); the real header is
    # sheet row 7, which is index 4 once the empty rows above the title are trimmed.
    assert file2[0].trace["header"]["row_index"] == 4
    assert file2[0].trace["header"]["alias_rate"] == 1.0


def test_header_with_a_gutter_hole_is_still_matched(file1):
    # File1's header spans B2:J2 with F2 empty. The empty cell must not count against it.
    assert file1[0].trace["header"]["alias_rate"] == 1.0
    assert "1 empty gutter column(s) dropped" in file1[0].trace["notes"]


def test_error_cell_is_nulled_before_scoring(file2):
    assert file2[0].trace["error_cells_nulled"] == ["A1=#VALUE!"]


def test_banner_and_footer_rows_are_stripped(file2):
    dropped = {row["classification"] for row in file2[0].trace["dropped_rows"]}
    assert {"BANNER", "FOOTER", "SUBTOTAL"} <= dropped
    contents = " ".join(row["content"] for row in file2[0].trace["dropped_rows"])
    assert "Confidential" in contents and "End of Report" in contents


def test_mid_table_subtotals_are_stripped(file3):
    subtotals = [
        row for row in file3[0].trace["dropped_rows"] if row["classification"] == "SUBTOTAL"
    ]
    assert len(subtotals) == 3
    assert all(row["content"].startswith("Subtotal - ") for row in subtotals)
    assert "Subtotal" not in file3[0].frame["profit_center_name"].str.cat(sep=" ")


def test_blank_row_inside_data_is_a_separator_not_a_terminator(file1):
    classifications = [row["classification"] for row in file1[0].trace["dropped_rows"]]
    assert "SEPARATOR" in classifications
    # All ten records survive the blank at row 8; a terminator would leave five.
    assert len(file1[0].frame) == 10


def test_a_total_row_that_carries_a_policy_number_is_kept():
    """A profit centre legitimately named 'Total ...' must not be mistaken for a subtotal."""
    rows = [
        CANONICAL_HEADER,
        ["Total Risk PC", 1001, "Metro", "Liberty", 100.0, "POL-1", "2026-01-01", 10.0],
        ["Subtotal - Total Risk PC", None, None, None, 100.0, None, None, None],
    ]
    result = extract_sheet(SheetGrid(name="S", rows=rows))
    assert len(result.frame) == 1
    assert result.frame.loc[0, "profit_center_name"] == "Total Risk PC"
    assert result.trace["dropped_rows"][0]["classification"] == "SUBTOTAL"


def test_unrecognised_columns_are_kept_not_dropped():
    rows = [CANONICAL_HEADER + ["Underwriter Notes"],
            ["East Zone PC", 1, "Metro", "Liberty", 1.0, "POL-1", "2026-01-01", 5.0, "renewal"]]
    result = extract_sheet(SheetGrid(name="S", rows=rows))
    assert "underwriter_notes" in result.frame.columns
    assert result.trace["unmapped_columns"] == ["underwriter_notes"]


def test_identifier_columns_stay_strings_across_both_storage_styles(file1, file5):
    """ProfitCenterNumber is numeric 1005 in one file and text 'PC0001' in another."""
    assert file1[0].frame["profit_center_number"].iloc[0] == "1005"
    assert file5[0].frame["profit_center_number"].iloc[0] == "PC0001"


def test_dates_normalise_to_iso_from_both_text_and_datetime(file1, file5):
    assert file1[0].frame["accounting_effective_date"].iloc[0] == "2026-01-01"
    assert file5[0].frame["accounting_effective_date"].iloc[0] == "2026-01-01"


def test_commission_is_left_as_a_whole_number_percent(file1):
    assert file1[0].frame["commission_pct"].iloc[0] == pytest.approx(11.06)


def test_zip_code_regains_its_leading_zero(file6):
    zips = file6[1].frame["zip_code"].tolist()
    assert "08085" in zips


def test_unparseable_values_are_reported_not_silently_nulled():
    rows = [CANONICAL_HEADER,
            ["East Zone PC", 1, "Metro", "Liberty", "n/a", "POL-1", "not a date", 5.0]]
    result = extract_sheet(SheetGrid(name="S", rows=rows))
    reasons = {failure["reason"] for failure in result.trace["coercion_failures"]}
    assert reasons == {"not numeric", "unparseable date"}
    assert pd.isna(result.frame.loc[0, "premium"])


def test_duplicate_policy_numbers_are_flagged_within_a_sheet():
    rows = [CANONICAL_HEADER,
            ["A", 1, "M", "L", 1.0, "POL-1", "2026-01-01", 5.0],
            ["B", 2, "M", "L", 2.0, "POL-1", "2026-01-02", 5.0]]
    result = extract_sheet(SheetGrid(name="S", rows=rows))
    checks = {finding["check"] for finding in result.trace["validation"]}
    assert "duplicate_policy_number_within_sheet" in checks
    # Flagged, never dropped.
    assert len(result.frame) == 2


def test_empty_sheet_is_handled(file1):
    result = extract_sheet(SheetGrid(name="Blank", rows=[[None, None], [None, None]]))
    assert result.frame.empty
    assert "nothing extracted" in result.trace["notes"][0]


def test_premium_reconciles_with_the_grand_total_the_file_declares(file2):
    """The junk we strip is the oracle for the data we keep."""
    grand_total = next(
        row for row in file2[0].trace["dropped_rows"] if row["classification"] == "SUBTOTAL"
    )
    declared = float(grand_total["content"].split("|")[4].strip())
    assert file2[0].frame["premium"].sum() == pytest.approx(declared, abs=0.01)


def test_premiums_reconcile_with_each_stripped_subtotal(file3):
    frame = file3[0].frame
    for row in file3[0].trace["dropped_rows"]:
        if row["classification"] != "SUBTOTAL":
            continue
        parts = [part.strip() for part in row["content"].split("|")]
        centre = parts[0].removeprefix("Subtotal - ")
        declared = float(parts[4])
        actual = frame.loc[frame["profit_center_name"] == centre, "premium"].sum()
        assert actual == pytest.approx(declared, abs=0.01), centre

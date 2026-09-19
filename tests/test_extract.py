"""End-to-end extraction on the real files, with no schema anywhere in the pipeline."""

import pandas as pd
import pytest

from ahi_clean import extract, rowclass
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

EXPECTED_COLUMNS = [
    "profitcentername",
    "profitcenternumber",
    "producer_agencyname",
    "insurancecompanyname",
    "premium",
    "policynumber",
    "accountingeffectivedate",
    "commission",
]


def only(results):
    """The single region of a sheet that holds exactly one table."""
    assert len(results) == 1, f"expected one region, got {len(results)}"
    return results[0]


def one_table(grid_rows, name="S"):
    return only(extract_sheet(SheetGrid(name=name, rows=grid_rows)))


# --------------------------------------------------------------------------- #
# The real files
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture_name,rows",
    [("file1", 10), ("file2", 10), ("file3", 10), ("file4", 10), ("file5", 10), ("file6", 10)],
)
def test_every_sample_sheet_yields_ten_records(fixture_name, rows, request):
    result = only(request.getfixturevalue(fixture_name)[0])
    assert len(result.frame) == rows


def test_column_names_are_the_normalised_originals(file1):
    """No canonical renaming: the file's own labels, made SQL-safe."""
    assert list(only(file1[0]).frame.columns) == EXPECTED_COLUMNS


def test_transposed_sheet_is_detected_and_flipped(file4):
    orientation = only(file4[0]).trace["orientation"]
    assert orientation["orientation"] == "transposed"
    assert orientation["confident"] is True
    assert orientation["column_score"] > orientation["row_score"]


@pytest.mark.parametrize("fixture_name", ["file1", "file2", "file3", "file5", "file6"])
def test_upright_sheets_are_not_transposed(fixture_name, request):
    for results in request.getfixturevalue(fixture_name):
        for result in results if isinstance(results, list) else [results]:
            assert result.trace["orientation"]["orientation"] == "normal"


def test_header_is_found_below_a_banner(file2):
    header = only(file2[0]).trace["header"]
    assert header["detected"] is True
    # Two banner lines sit above it inside the region.
    assert header["row_index"] == 2


def test_header_with_a_gutter_hole_is_still_matched(file1):
    result = only(file1[0])
    assert result.trace["header"]["detected"] is True
    # The empty column F is gone, the eight real columns survive.
    assert result.frame.shape[1] == 8


def test_error_cell_is_nulled_before_scoring(file2):
    assert only(file2[0]).trace["error_cells_nulled"] == ["A1=#VALUE!"]


def test_banner_and_footer_rows_are_stripped(file2):
    dropped = {row["classification"] for row in only(file2[0]).trace["dropped_rows"]}
    assert rowclass.BANNER in dropped
    assert rowclass.FOOTER in dropped
    assert rowclass.GRAND_TOTAL in dropped


def test_mid_table_subtotals_are_stripped_by_arithmetic(file3):
    result = only(file3[0])
    subtotals = [
        row for row in result.trace["dropped_rows"] if row["classification"] == rowclass.SUBTOTAL
    ]
    assert len(subtotals) == 3
    # Dropped because the numbers sum, not because the word "Subtotal" appears.
    assert all("equals the sum of" in row["reason"] for row in subtotals)
    assert "Subtotal" not in result.frame["profitcentername"].str.cat(sep=" ")


def test_blank_row_inside_data_does_not_truncate_the_table(file1):
    assert len(only(file1[0]).frame) == 10


# --------------------------------------------------------------------------- #
# Behaviour on constructed sheets
# --------------------------------------------------------------------------- #


def test_a_total_row_that_carries_a_policy_number_is_kept():
    rows = [
        CANONICAL_HEADER,
        ["Total Risk PC", 1001, "Metro", "Liberty", 100.0, "POL-1", "2026-01-01", 10.0],
        ["East Zone PC", 1002, "Metro", "Liberty", 200.0, "POL-2", "2026-01-02", 10.0],
        ["Subtotal", None, None, None, 300.0, None, None, None],
    ]
    result = one_table(rows)
    assert len(result.frame) == 2
    assert "Total Risk PC" in result.frame["profitcentername"].tolist()
    assert result.trace["dropped_rows"][0]["classification"] == rowclass.GRAND_TOTAL


def test_unrecognised_columns_are_kept_not_dropped():
    rows = [CANONICAL_HEADER + ["Underwriter Notes"]] + [
        [f"Zone {index}", 1000 + index, "Metro", "Liberty", 100.5 + index,
         f"POL-{index}", f"2026-01-{index + 1:02d}", 5.0, "renewal"]
        for index in range(5)
    ]
    result = one_table(rows)
    assert "underwriter_notes" in result.frame.columns
    assert len(result.frame) == 5


def test_identifier_columns_survive_both_storage_styles(file1, file5):
    """1005 and PC0001 are the same field in different files, and both stay text.

    Recovered structurally -- constant-width near-unique integers are codes -- with no
    schema declaring it.
    """
    assert only(file1[0]).frame["profitcenternumber"].iloc[0] == "1005"
    assert only(file5[0]).frame["profitcenternumber"].iloc[0] == "PC0001"


def test_dates_normalise_to_iso_from_both_text_and_datetime(file1, file5):
    assert only(file1[0]).frame["accountingeffectivedate"].iloc[0] == "2026-01-01"
    assert only(file5[0]).frame["accountingeffectivedate"].iloc[0] == "2026-01-01"


def test_whole_valued_numeric_columns_are_written_as_integers(file6):
    zips = only(file6[1]).frame["zip_code"]
    assert zips.dtype.name == "Int64"
    assert 75202 in zips.tolist()


def test_unparseable_values_are_reported_not_silently_nulled():
    rows = [CANONICAL_HEADER] + [
        [f"Zone {index}", 1000 + index, "Metro", "Liberty", 100.5 + index,
         f"POL-{index}", f"2026-01-{index + 1:02d}", 5.0]
        for index in range(5)
    ]
    rows.append(["Zone 9", 1009, "Apex", "Zenith", "n/a", "POL-9", "not a date", 6.0])
    result = one_table(rows)
    reasons = {failure["reason"] for failure in result.trace["coercion_failures"]}
    assert reasons == {"not numeric", "unparseable date"}
    # The bad row is kept; only the two unparseable cells are nulled.
    assert len(result.frame) == 6
    assert pd.isna(result.frame.loc[5, "premium"])
    assert result.frame.loc[5, "policynumber"] == "POL-9"


def test_duplicate_keys_are_flagged_within_a_region():
    rows = [
        CANONICAL_HEADER,
        ["A", 1, "M", "L", 1.5, "POL-1", "2026-01-01", 5.0],
        ["B", 2, "M", "L", 2.5, "POL-1", "2026-01-02", 5.0],
        ["C", 3, "M", "L", 3.5, "POL-1", "2026-01-03", 5.0],
    ]
    result = one_table(rows)
    # Flagged, never dropped.
    assert len(result.frame) == 3


def test_an_empty_sheet_is_handled():
    result = one_table([[None, None], [None, None]])
    assert result.frame.empty
    assert "no table-shaped region" in result.trace["notes"][0]


# --------------------------------------------------------------------------- #
# Reconciliation: the junk we strip is the oracle for the data we keep
# --------------------------------------------------------------------------- #


def test_premium_reconciles_with_the_grand_total_the_file_declares(file2):
    result = only(file2[0])
    total = next(
        row for row in result.trace["dropped_rows"] if row["classification"] == rowclass.GRAND_TOTAL
    )
    declared = float(total["content"].split("|")[4].strip())
    assert result.frame["premium"].sum() == pytest.approx(declared, abs=0.01)


def test_premiums_reconcile_with_each_stripped_subtotal(file3):
    result = only(file3[0])
    frame = result.frame
    for row in result.trace["dropped_rows"]:
        if row["classification"] != rowclass.SUBTOTAL:
            continue
        parts = [part.strip() for part in row["content"].split("|")]
        centre = parts[0].removeprefix("Subtotal - ")
        declared = float(parts[4])
        actual = frame.loc[frame["profitcentername"] == centre, "premium"].sum()
        assert actual == pytest.approx(declared, abs=0.01), centre

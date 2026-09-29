"""Every edge case the cleaner meets is flagged, in the column's ``type_flag``.

CHECK marks a judgement call or values lost; INFO marks a deliberate, certain change.
Built in memory (or in a temporary file for the CSV reader), so swapping the sample
corpus cannot break what these pin.
"""

from types import SimpleNamespace

import polars as pl
import pytest

from ahi_clean import coerce, extract, geometry
from ahi_clean.orchestrate import plan_workbook
from ahi_clean.reader import SheetGrid, read_workbook

from conftest import HEADER, one, records


def flags_of(result, column):
    return " | ".join(result.trace["flags"].get(column, []))


def all_flags(result):
    return " | ".join(flag for flags in result.trace["flags"].values() for flag in flags)


# --------------------------------------------------------------------------- #
# Numbers: decimal mark, trailing minus, scientific notation
# --------------------------------------------------------------------------- #


def test_a_european_decimal_comma_is_read_correctly():
    """1.234,56 used to become 1.23456, and 99,5 became 995 -- silently."""
    result = coerce.coerce_column("amount", ["1.234,56", "2.500,00", "99,5"])
    assert result.series.to_list() == [1234.56, 2500.0, 99.5]
    assert any(flag.startswith("INFO: European number format") for flag in result.flags)


def test_us_grouping_is_read_and_noted():
    result = coerce.coerce_column("amount", ["1,234.56", "2,500.00", "99.5"])
    assert result.series.to_list() == [1234.56, 2500.0, 99.5]
    assert any("thousands separators removed" in flag for flag in result.flags)


def test_one_thousand_or_one_point_something_is_a_flagged_tie():
    result = coerce.coerce_column("amount", ["1,234", "2,500", "3,750"])
    assert result.series.to_list() == [1234, 2500, 3750]
    assert any(flag.startswith("CHECK: values such as 1,234 fit both") for flag in result.flags)


def test_mixed_us_and_european_formats_are_flagged():
    result = coerce.coerce_column("amount", ["1,234.56", "2.500,75", "3,750.10"])
    assert any(flag.startswith("CHECK: mixes US and European") for flag in result.flags)


def test_a_trailing_minus_is_a_negative():
    result = coerce.coerce_column("amount", ["500-", "200", "1,000.50-"])
    assert result.series.to_list() == [-500.0, 200.0, -1000.5]
    assert any("trailing minus" in flag for flag in result.flags)


def test_scientific_notation_is_kept_and_flagged():
    result = coerce.coerce_column("ref", ["1.23E+05", "4.56E+05", "7.89E+05"])
    assert result.series.dtype == pl.String
    assert any("scientific notation" in flag for flag in result.flags)


# --------------------------------------------------------------------------- #
# Types: mixed columns, eight-digit numbers under an identifier header
# --------------------------------------------------------------------------- #


def test_a_column_partly_of_dates_is_flagged():
    result = coerce.coerce_column("when", ["2026-01-08", "2026-01-09", "pending", "n/a", "tbd"])
    assert result.series.dtype == pl.String
    assert any("read as dates, too few" in flag for flag in result.flags)


def test_ordinary_text_is_not_flagged_as_mixed():
    assert coerce.coerce_column("name", ["Alice", "Bob", "Carol"]).flags == []


@pytest.mark.parametrize(
    "label, dtype, words",
    [
        ("PolicyNo", pl.String, "names an identifier"),
        ("account_number", pl.String, "names an identifier"),
        ("TxnDate", pl.Date, "names a date"),
        ("Value", pl.Date, "neither a date nor an identifier"),
    ],
)
def test_eight_digit_numbers_follow_the_header(label, dtype, words):
    result = coerce.coerce_column("c", [20240101, 20240215, 20240330], label)
    assert result.series.dtype == dtype
    assert result.flags[0].startswith("CHECK:") and words in result.flags[0]


# --------------------------------------------------------------------------- #
# The file: separator and encoding
# --------------------------------------------------------------------------- #


def test_a_separator_tie_is_flagged(tmp_path):
    path = tmp_path / "tie.csv"
    path.write_text("a,b;c\n1,2;3\n4,5;6\n7,8;9\n", encoding="utf-8")
    [grid] = read_workbook(path)
    assert any("separator was a close call" in flag for flag in grid.read_flags)


def test_a_windows_1252_file_is_noted(tmp_path):
    path = tmp_path / "cp.csv"
    path.write_bytes("name,city\nJosé,Málaga\nAna,León\n".encode("cp1252"))
    [grid] = read_workbook(path)
    assert any("read as Windows-1252" in flag for flag in grid.read_flags)


def test_a_latin_1_fallback_is_a_check(tmp_path):
    path = tmp_path / "latin.csv"
    # 0x81 is undefined in Windows-1252, so only the Latin-1 fallback can read this.
    path.write_bytes(b"name,city\nJos\xe9,M\x81laga\nAna,Leon\n")
    [grid] = read_workbook(path)
    assert any(flag.startswith("CHECK:") and "Latin-1" in flag for flag in grid.read_flags)


def test_file_flags_reach_every_column():
    grid = SheetGrid(name="f", rows=[HEADER] + records(6))
    grid.read_flags = ["CHECK: read oddity"]
    [result] = extract.extract_sheet(grid)
    assert all("CHECK: read oddity" in flags_of(result, name) for name in result.frame.columns)


# --------------------------------------------------------------------------- #
# The sheet: error cells, formulas, merged cells
# --------------------------------------------------------------------------- #


def test_excel_error_cells_are_flagged():
    grid = SheetGrid(name="s", rows=[HEADER] + records(6), error_cells=["E3=#REF!"])
    [result] = extract.extract_sheet(grid)
    assert "Excel error value" in flags_of(result, "premium")


def test_formulas_without_saved_results_are_flagged():
    grid = SheetGrid(name="s", rows=[HEADER] + records(6), uncached_formula_cells=["E4"])
    [result] = extract.extract_sheet(grid)
    assert "no saved result" in flags_of(result, "premium")


def test_merges_spanning_rows_are_noted_but_title_merges_are_not():
    tall = SheetGrid(name="s", rows=[HEADER] + records(6), merged_ranges=["A2:A4"])
    wide = SheetGrid(name="s", rows=[HEADER] + records(6), merged_ranges=["A1:H1"])
    assert "merged cell range" in all_flags(extract.extract_sheet(tall)[0])
    assert "merged cell range" not in all_flags(extract.extract_sheet(wide)[0])


# --------------------------------------------------------------------------- #
# The table: header confidence, alignment, boundaries, borrowed names
# --------------------------------------------------------------------------- #


def test_a_low_confidence_header_is_flagged():
    trace = {"flags": {}}
    found = SimpleNamespace(detected=True, score=0.58, multi_row=False)
    extract._flag_structure(["a", "b"], found, trace)
    assert "low confidence" in trace["flags"]["a"][0]


def test_header_and_data_that_cannot_be_lined_up_are_flagged():
    rows = [["Name", "Amount", "Code", None, None], ["a", 1, None, "x", 5]]
    found = SimpleNamespace(detected=True, row_index=0)
    trace = {"notes": []}
    extract._realign_gutters(["name", "amount", "code", "c4", "c5"], found, rows, rows[1:], 5, trace)
    assert trace["alignment_mismatch"] == {"header": [0, 1, 2], "data": [0, 1, 3, 4]}


def test_a_blank_header_cell_is_not_mistaken_for_misalignment():
    header = list(HEADER)
    header[3] = None
    result = one([header] + records(6))
    assert "alignment_mismatch" not in result.trace


def test_a_close_join_across_a_gap_is_recorded():
    anchor = [["x", 1, 2.5, "2026-01-01"] for _ in range(4)]
    candidate = [["y", 3, 4.5, None] for _ in range(4)]
    joined, note = geometry._join_decision(anchor, candidate, 4)
    assert joined and "only just match" in note


def test_boundary_close_calls_reach_every_column():
    trace = {"flags": {}}
    found = SimpleNamespace(detected=True, score=0.9, multi_row=False)
    region = SimpleNamespace(close_calls=["joined although they only just match"])
    extract._flag_structure(["a"], found, trace, None, region)
    assert "table boundary was a close call" in trace["flags"]["a"][0]


def test_equally_good_donor_sheets_are_flagged():
    first = one([HEADER] + records(6), name="Report")
    second = one([[f"{label} B" for label in HEADER]] + records(6, start=6), name="Report B")
    continuation = one(records(4, start=12), name="Continued")
    plan_workbook([first, second, continuation], "book")
    assert "matched equally well" in flags_of(continuation, "premium")


# --------------------------------------------------------------------------- #
# Rows: kept though incomplete, totals that did not add up
# --------------------------------------------------------------------------- #


def test_rows_kept_with_low_confidence_are_flagged():
    sparse = ["West Zone PC", None, None, None, 123.45, None, None, None]
    result = one([HEADER] + records(6) + [sparse] + records(3, start=6))
    assert "look incomplete" in flags_of(result, "premium")


def test_totals_that_did_not_add_up_are_flagged():
    total = ["Grand Total", None, None, None, 999999.99, None, None, None]
    result = one([HEADER] + records(6) + [total])
    assert "did not add up" in flags_of(result, "premium")

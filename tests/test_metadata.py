"""The metadata file: statistics chosen by the cleaner's own types, never re-guessed.

Built in memory, so swapping the sample corpus cannot break what these pin.
"""

import json
import re

import openpyxl
import polars as pl

from ahi_clean import metadata
from ahi_clean.cli import clean_workbook
from ahi_clean.orchestrate import plan_workbook

from conftest import HEADER, blanks, one, records, sheet


def rows_by_column(described: pl.DataFrame) -> dict:
    return {row["header_name"]: row for row in described.to_dicts()}


def described(result, excel_name="book.xlsx"):
    [output], _report = plan_workbook([result], "book")
    return rows_by_column(metadata.build(output.frame, excel_name, output.sheet_names))


# --------------------------------------------------------------------------- #
# Statistics follow the datatype
# --------------------------------------------------------------------------- #


def test_a_numeric_column_gets_min_max_and_an_exact_sum():
    frame = pl.DataFrame({"amount": [0.1, 0.2, 10000.55, None]})
    [row] = metadata.build(frame, "book.xlsx", ["Report"]).to_dicts()
    assert row["datatype"] == "Float64"
    assert (row["min"], row["max"]) == ("0.1", "10000.55")
    # 0.1 + 0.2 in floating point is 0.30000000000000004; the sum must be exact.
    assert row["sum"] == "10000.85"
    assert (row["count"], row["distinct_count"], row["total_row_count"]) == (3, 3, 4)
    assert row["null_percentage"] == "25.00"


def test_a_date_column_gets_min_and_max_but_no_sum():
    row = described(one([HEADER] + records(6)))["accountingeffectivedate"]
    assert row["datatype"] == "Date"
    assert (row["min"], row["max"]) == ("2026-01-01", "2026-01-06")
    assert row["sum"] == metadata.NOT_APPLICABLE


def test_a_code_column_is_text_and_gets_no_numeric_statistics():
    """Centre numbers look like integers; the cleaner kept them as text, from values alone."""
    row = described(one([HEADER] + records(6)))["profitcenternumber"]
    assert row["datatype"] == "String"
    assert (row["min"], row["max"], row["sum"]) == ("NA", "NA", "NA")
    assert row["distinct_count"] == 6


def test_null_percentage_is_counted_for_every_type():
    frame = pl.DataFrame(
        {"name": ["a", None, None, "d"], "amount": [1, None, 3, 4]},
        schema={"name": pl.String, "amount": pl.Int64},
    )
    by_column = rows_by_column(metadata.build(frame, "book.xlsx", ["Report"]))
    assert by_column["name"]["null_percentage"] == "50.00"
    assert by_column["amount"]["null_percentage"] == "25.00"
    assert by_column["amount"]["sum"] == "8"


# --------------------------------------------------------------------------- #
# Sheet identity
# --------------------------------------------------------------------------- #


def test_an_appended_table_is_described_sheet_by_sheet():
    january = one([HEADER] + records(6), name="Jan")
    february = one([HEADER] + records(4, start=6), name="Feb")
    [output], _report = plan_workbook([january, february], "book")
    rows = metadata.build(output.frame, "book.xlsx", output.sheet_names).to_dicts()

    premium = [row for row in rows if row["header_name"] == "premium"]
    assert [row["sheet_name"] for row in premium] == ["Jan", "Feb"]
    assert [row["total_row_count"] for row in premium] == [6, 4]
    assert {row["excel_name"] for row in rows} == {"book.xlsx"}


def test_a_second_table_on_a_sheet_carries_its_own_types_and_the_sheet_name():
    adjustments = [["Code", "Label", "Amount"]] + [
        [f"C{index}", f"note {index}", 10.5 * (index + 1)] for index in range(5)
    ]
    first, second = sheet([HEADER] + records(6) + blanks(3) + [row + [None] * 5 for row in adjustments])
    assert second.label == "Report_r2"

    outputs, _report = plan_workbook([first, second], "book")
    table = [output for output in outputs if output.sheets == [second.label]][0]
    by_column = rows_by_column(metadata.build(table.frame, "book.xlsx", table.sheet_names))
    assert by_column["amount"]["datatype"] == "Float64"
    assert by_column["amount"]["sum"] == "157.5"
    assert by_column["amount"]["sheet_name"] == "Report"


def test_a_continuation_with_adopted_names_is_typed_by_its_own_values():
    first = one([HEADER] + records(6), name="Report")
    second = one(records(4, start=6), name="Continued")
    outputs, _report = plan_workbook([first, second], "book")
    table = [output for output in outputs if output.sheets == [second.label]][0]
    by_column = rows_by_column(metadata.build(table.frame, "book.xlsx", table.sheet_names))
    assert by_column["premium"]["datatype"] == "Float64"
    assert by_column["accountingeffectivedate"]["datatype"] == "Date"
    assert by_column["premium"]["sheet_name"] == "Continued"


# --------------------------------------------------------------------------- #
# End to end: job ids, audit names, and the inference check
# --------------------------------------------------------------------------- #


def _workbook(path):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, start in (("Jan", 0), ("Feb", 6)):
        worksheet = book.create_sheet(name)
        for row in [HEADER] + records(6, start=start):
            worksheet.append(row)
    book.save(path)
    return path


def test_each_csv_is_written_with_its_metadata_under_one_job_id(tmp_path):
    source = _workbook(tmp_path / "book.xlsx")
    outcome = clean_workbook(source, tmp_path / "cleaned", tmp_path / "audit")

    [output] = outcome["outputs"]
    assert re.fullmatch(r"book_[0-9a-f-]{36}\.csv", output.file)
    assert output.metadata_file == f"book_metadata_{output.job_id}.csv"
    names = {path.name for path in (tmp_path / "cleaned").iterdir()}
    assert names == {output.file, output.metadata_file}

    report = json.loads(outcome["report"].read_text(encoding="utf-8"))
    [entry] = report["outputs"]
    assert (entry["file"], entry["metadata_file"], entry["job_id"]) == (
        output.file, output.metadata_file, output.job_id,
    )


def test_inferred_datatype_shows_what_a_loader_would_guess(tmp_path):
    """The cleaner keeps centre codes as text; a loader inferring from the CSV would not."""
    source = _workbook(tmp_path / "book.xlsx")
    outcome = clean_workbook(source, tmp_path / "cleaned", tmp_path / "audit")
    [output] = outcome["outputs"]
    rows = pl.read_csv(tmp_path / "cleaned" / output.metadata_file).to_dicts()

    code = [row for row in rows if row["header_name"] == "profitcenternumber"][0]
    assert (code["datatype"], code["inferred_datatype"]) == ("String", "Int64")
    date = [row for row in rows if row["header_name"] == "accountingeffectivedate"][0]
    assert (date["datatype"], date["inferred_datatype"]) == ("Date", "Date")
    assert list(rows[0]) == metadata.FIELDNAMES


def test_statistics_that_do_not_apply_are_written_as_na():
    frame = pl.DataFrame(
        {"name": ["a", "b"], "empty": [None, None]},
        schema={"name": pl.String, "empty": pl.Float64},
    )
    text = metadata.build(frame, "book.xlsx", ["Report"]).write_csv()
    for line in text.splitlines()[1:]:
        assert ",NA,NA,NA," in line, line

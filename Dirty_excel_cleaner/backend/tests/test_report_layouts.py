"""Report layouts found in real profit-center files, rebuilt in memory.

Each test names the layout, never a file: merged bands, empty templates, dense totals,
side-by-side summaries, headers spaced from their records, text exports pasted into one
column. Every one of these produced a plausible but wrong CSV before.
"""

import openpyxl

from ahi_clean import delimited, rowclass
from ahi_clean.reader import read_sheet
from conftest import kinds, one, sheet


def _carriers(count, months=3):
    """Ledger rows: each carrier fills one month only, so records are sparse."""
    rows = []
    for index in range(count):
        values = [None] * months
        values[index % months] = 100.0 * (index + 1)
        rows.append([f"Carrier {chr(65 + index)}", f"00{index}", "APEX"] + values)
    return rows


# --- merged cells ------------------------------------------------------------------


def test_a_merge_across_columns_stays_one_cell_but_a_merge_down_fills_down(tmp_path):
    path = tmp_path / "merged.xlsx"
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Report title", None, None])
    ws.append(["Region", "Policy", "Premium"])
    ws.append(["East", "P-1", 10])
    ws.append([None, "P-2", 20])
    ws.merge_cells("A1:C1")  # a title: layout
    ws.merge_cells("A3:A4")  # a region shared by two records: data
    book.save(path)
    grid = read_sheet(path, ws.title)
    assert grid.rows[0] == ["Report title", None, None]
    assert grid.rows[3][0] == "East"


# --- orientation -------------------------------------------------------------------


def test_an_empty_template_is_not_read_sideways():
    """Carriers down the side, figures not filled in yet: no records, not a sideways table."""
    rows = [["CARRIER", "PIF", "PREMIUM"]] + [[name, None, None] for name in
                                             ["Travelers", "Safeco", "Progressive", "Hartford",
                                              "Chubb", "Zurich", "Markel", "Allianz"]]
    rows.append(["GRAND TOTAL", 0, 0])
    for result in sheet(rows):
        assert (result.trace.get("orientation") or {}).get("orientation") != "transposed"


def test_a_short_wide_report_with_a_clear_header_stays_upright():
    header = ["Record Type", "Policy Number", "Policy Year", "Status", "Carrier", "Effective",
              "Expiration", "Final Premium", "Gross Premium", "Line", "Fee Type", "Broker"]
    rows = [header,
            ["ERM", "172-26-NC", "2026-2027", "Active", "Series 1", "8/15/2026", "8/14/2027", 503976, None, None, None, "Direct"],
            ["ERM", "196-26-NC", "2026-2027", "Active", "Series 1", "8/1/2026", "7/30/2027", 1263847, None, None, None, "Direct"]]
    result = one(rows)
    assert len(result.frame) == 2
    assert result.frame.columns[1] == "policy_number"


# --- totals ------------------------------------------------------------------------


def test_a_total_row_fuller_than_its_records_is_dropped_when_it_sums_them():
    """Each carrier fills one month; the total fills all three, so it is the densest row."""
    body = _carriers(5)
    total = ["Total", None, None] + [sum(row[3 + m] or 0 for row in body) for m in range(3)]
    result = one([["Carrier", "AM Best", "Billing", "Jan", "Feb", "Mar"]] + body + [total])
    assert len(result.frame) == 5
    assert rowclass.GRAND_TOTAL in kinds(result)


def test_a_last_record_equal_to_one_column_sum_is_still_a_record():
    rows = [["Name", "Amount", "Code"], ["A", 10.0, "X-1"], ["B", 20.0, "X-2"], ["C", 30.0, "X-3"]]
    assert len(one(rows).frame) == 3


def test_a_total_with_a_ratio_column_is_still_a_total():
    rows = [["Carrier", "Premium", "Revenue", "Rate"],
            ["A", 1000.0, 150.0, 0.15], ["B", 2000.0, 400.0, 0.2], ["C", 500.0, 50.0, 0.1],
            ["Grand Total", 3500.0, 600.0, 0.171]]
    result = one(rows)
    assert len(result.frame) == 3


# --- label-only rows ----------------------------------------------------------------


def test_a_label_only_row_between_records_is_kept():
    rows = [["Carrier", "PIF", "Premium"], ["Travelers", 26, 27698.0], ["Mercury", None, None],
            ["Safeco", 32, 37050.47], ["Progressive", 63, 63111.5]]
    result = one(rows)
    assert "Mercury" in result.frame["carrier"].to_list()


def test_report_criteria_after_the_last_record_are_footer():
    header = ["Agent", "Policy", "Premium", "Fees", "State"]
    records = [[f"Agent {i}", f"POL-{i}", 100.0 + i, 5.0, "NY"] for i in range(6)]
    criteria = [[None, "Primary Grouping:", "Agent", None, None],
                [None, "Filters:", "Effective = July", None, None]]
    result = one([header] + records + criteria)
    assert len(result.frame) == 6


# --- side by side ---------------------------------------------------------------------


def test_summaries_side_by_side_with_repeated_labels_are_split():
    rows = [["Carrier", "Premium", "Revenue", None, "Month", "Premium", "Revenue"]]
    for index in range(6):
        rows.append([f"Carrier {index}", 100.0 + index, 10.0 + index, None,
                     f"M{index + 1}", 200.0 + index, 20.0 + index])
    found = sheet(rows)
    assert len(found) == 2
    assert found[1].frame.columns[0] == "month"


# --- header spaced from its records -----------------------------------------------------


def test_a_header_over_a_blank_row_that_labels_empty_fields_stays_one_table():
    header = ["Group", "Policy", "Entry", "Contract", "State", "Class", "Premium", "Agent"]
    records = [[f"G{i}", f"POL-{i}", f"2026-07-{i + 1:02d}", None, None, f"00{i}", 100.0 + i, f"Agent {i}"]
               for i in range(8)]
    result = one([header, [None] * 8] + records)
    assert len(result.frame) == 8
    assert list(result.frame.columns)[:2] == ["group", "policy"]


# --- a text export pasted into one column -------------------------------------------------


def test_a_delimited_export_inside_column_a_is_split_back():
    lines = ["id|name|city|state"] + [f"{i:04d}|Name {i}|Eden|MN" for i in range(8)]
    rows = [[line, None] for line in lines]
    rows[3] = ["0002|Name 2", "Suite 100|Eden|MN"]  # Excel cut this line on a comma
    unpacked, delimiter = delimited.unpack_column(rows)
    assert delimiter == "|"
    assert unpacked[0] == ["id", "name", "city", "state"]
    assert len(unpacked) == 9
    result = one(rows)
    assert list(result.frame.columns) == ["id", "name", "city", "state"]
    assert len(result.frame) == 8


def test_prose_in_column_a_is_not_unpacked():
    rows = [[f"Note {i}: commissions, fees and adjustments are provisional"] for i in range(8)]
    assert delimited.unpack_column(rows) is None


# --- headings broken over lines (PDF converted to Excel) --------------------------------


def test_a_heading_broken_over_three_lines_is_joined():
    rows = [["Applicant", "Policy", "Face", "Close/"],
            ["Name", "Number", "Amoun", "Placed"],
            [None, None, "t", "Date"]]
    rows += [[f"Name {i}", f"P{1000 + i}", 250000 + i, f"2026-07-{i + 1:02d}"] for i in range(8)]
    result = one(rows)
    assert list(result.frame.columns) == ["applicant_name", "policy_number", "face_amount", "close_placed_date"]
    assert len(result.frame) == 8


def test_an_all_text_table_keeps_its_first_record():
    rows = [["Name", "Carrier", "Status", "Agent"]] + [
        [f"Client {i}", "Banner", "In Force", f"Agent {i}"] for i in range(8)]
    result = one(rows)
    assert len(result.frame) == 8
    assert result.frame["name"][0] == "Client 0"


# --- one-column lists and notes ----------------------------------------------------------


def test_a_column_of_dates_alone_is_a_list_not_banners():
    import datetime
    rows = [[datetime.datetime(2026, 7, day)] for day in range(1, 21)]
    result = one(rows)
    assert result.frame.shape == (20, 1)


def test_a_sheet_of_code_comments_is_not_a_table():
    rows = [["Non Tricare", "Tricare Checks", "Tricare EFT"],
            ["--Remittance query, updated", None, "--Updated 2026-07-01 by ops"]]
    assert all(result.frame.is_empty() for result in sheet(rows))

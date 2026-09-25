"""Extraction behaviour, pinned with sheets built in memory.

These tests never name a corpus file. What the pipeline must do does not change when
the sample workbooks are swapped, so the tests that pin it should not either.
"""

import polars as pl
import pytest

from ahi_clean import rowclass

from conftest import HEADER, NAMES, blanks, kinds, one, record, records, sheet

# --------------------------------------------------------------------------- #
# Header location
# --------------------------------------------------------------------------- #


def test_a_clean_table_extracts_as_written():
    result = one([HEADER] + records(6))
    assert list(result.frame.columns) == NAMES
    assert len(result.frame) == 6


def test_a_header_far_below_the_top_is_found():
    rows = [["Company Report"] + [None] * 7] + blanks(12) + [HEADER] + records(6)
    result = one(rows)
    assert result.trace["header"]["detected"]
    assert len(result.frame) == 6


def test_a_merged_title_band_above_the_header_is_not_the_header():
    banner = [["QUARTERLY REPORT"] * 8]
    result = one(banner + [HEADER] + records(10))
    assert list(result.frame.columns) == NAMES
    assert len(result.frame) == 10


def test_a_note_between_header_and_data_is_dropped():
    rows = [HEADER, ["Note: commissions are provisional"] + [None] * 7] + records(6)
    result = one(rows)
    assert len(result.frame) == 6
    assert rowclass.FOOTER in kinds(result)


def test_labels_split_over_two_rows_are_combined():
    top = ["Profit Center", "Profit Center", "Producer", "Insurance", "Premium", "Policy", "Accounting", "Commission"]
    bottom = ["Name", "Number", "Agency", "Company", None, "Number", "Date", "%"]
    result = one([top, bottom] + records(10))
    assert result.trace["header"]["multi_row"] is True
    assert result.frame.columns[0] == "profit_center_name"
    assert result.frame.columns[4] == "premium"
    assert len(result.frame) == 10


def test_a_header_cell_with_no_label_keeps_its_column_positionally():
    holed = HEADER[:5] + [None] + HEADER[6:]
    result = one([holed] + records(6))
    assert "column_6" in result.frame.columns
    assert result.frame["column_6"][0].startswith("POL-")


def test_a_region_with_no_header_at_all_is_reported_not_invented():
    """Naming a data row as the header is worse than admitting none was found."""
    result = one(records(10))
    assert result.trace["header"]["detected"] is False
    assert list(result.frame.columns)[0] == "column_1"


# --------------------------------------------------------------------------- #
# Gutters and alignment
# --------------------------------------------------------------------------- #


def test_body_is_reseated_when_it_disagrees_with_the_header_about_gutters():
    """The worst failure mode: values silently landing under the wrong label."""
    body = [row[:3] + [None] + row[3:6] + [None] + row[6:] for row in records(6)]
    result = one([HEADER + [None, None]] + body)
    assert list(result.frame.columns) == NAMES
    assert result.frame["insurancecompanyname"].null_count() == 0
    assert result.frame["policynumber"][0].startswith("POL-")
    assert any("re-seated" in note for note in result.trace["notes"])


@pytest.mark.parametrize("gap", [1, 2, 3])
def test_an_empty_gutter_column_is_dropped(gap):
    widen = lambda row: row[:4] + [None] * gap + row[4:]  # noqa: E731
    result = one([widen(HEADER)] + [widen(row) for row in records(6)])
    assert list(result.frame.columns) == NAMES


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gap", [1, 2, 5, 12])
def test_blank_runs_of_any_length_keep_one_table(gap):
    rows = [HEADER] + records(3) + blanks(gap) + records(3, start=3)
    result = one(rows)
    assert len(result.frame) == 6


def test_a_sparse_row_between_sections_does_not_wall_off_the_table():
    subtotal = ["Subtotal", None, None, None, 21111.11, None, None, None]
    rows = [HEADER] + records(2) + blanks(4) + [subtotal] + blanks(2) + records(3, start=2)
    result = one(rows)
    assert len(result.frame) == 5


def test_a_genuinely_different_table_below_is_a_separate_region():
    second = [["Region", "Total", None, None, None, None, None, None]] + [
        ["East", 500.0, None, None, None, None, None, None],
        ["West", 600.0, None, None, None, None, None, None],
    ]
    found = sheet([HEADER] + records(4) + blanks(2) + second)
    assert len(found) == 2


def test_side_by_side_tables_of_different_length_are_split():
    left = [HEADER[:4]] + [record(index)[:4] for index in range(5)]
    right = [["Producer", "State"]] + [[f"Agency {index}", "NY"] for index in range(3)] + [[None, None]] * 2
    rows = [left[index] + [None, None] + right[index] for index in range(len(left))]
    found = sheet(rows)
    assert len(found) == 2


def test_a_banner_block_fenced_by_blanks_is_not_a_table():
    rows = (
        [["Acme Corp"] + [None] * 7, ["Confidential"] + [None] * 7]
        + blanks(2)
        + [HEADER]
        + records(5)
    )
    result = one(rows)
    assert len(result.frame) == 5


# --------------------------------------------------------------------------- #
# Row classification
# --------------------------------------------------------------------------- #


def test_a_repeated_header_is_removed():
    rows = [HEADER] + records(3) + [HEADER] + records(3, start=3)
    result = one(rows)
    assert len(result.frame) == 6
    assert rowclass.REPEATED_HEADER in kinds(result)


def test_a_header_restated_with_different_aliases_is_removed():
    variant = ["Profit Centre", "PC Number", "Producer", "Insurance Co",
               "Premium ($)", "Policy No.", "Effective Dt", "Comm %"]
    rows = [HEADER] + records(3) + [variant] + records(3, start=3)
    result = one(rows)
    assert len(result.frame) == 6
    assert rowclass.REPEATED_HEADER in kinds(result)


def test_a_partial_header_fragment_is_removed():
    fragment = ["Policy Summary", None, None, None, "Premium", "PolicyNumber", None, None]
    rows = [HEADER] + records(3) + [fragment] + records(3, start=3)
    result = one(rows)
    assert len(result.frame) == 6
    assert rowclass.REPEATED_HEADER in kinds(result)


def test_a_subtotal_is_proven_by_arithmetic():
    data = records(3)
    total = sum(row[4] for row in data)
    rows = [HEADER] + data + [["Subtotal", None, None, None, total, None, None, None]]
    result = one(rows)
    assert len(result.frame) == 3
    proven = [row for row in result.trace["dropped_rows"] if "TOTAL" in row["classification"]]
    assert proven and all("equals the sum of" in row["reason"] for row in proven)


def test_a_total_whose_figure_is_wrong_is_dropped_into_its_own_class():
    """Real reports carry stale totals; keeping one corrupts every downstream sum."""
    rows = [HEADER] + records(4) + [["TOTAL West Zone", None, None, None, 99.99, None, None, None]]
    result = one(rows)
    assert len(result.frame) == 4
    assert rowclass.UNVERIFIED_TOTAL in kinds(result)
    flagged = [row for row in result.trace["dropped_rows"]
               if row["classification"] == rowclass.UNVERIFIED_TOTAL]
    assert all("does not reconcile" in row["reason"] for row in flagged)


def test_a_sparse_row_with_no_total_wording_is_kept():
    """A partially filled record is ordinary; losing one silently is the worst failure."""
    partial = [None, None, None, None, 55.5, "POL-999999", None, None]
    result = one([HEADER] + records(4) + [partial])
    assert len(result.frame) == 5
    assert "POL-999999" in result.frame["policynumber"].to_list()


def test_a_full_row_named_total_is_plain_data():
    row = ["Total Risk PC", 1099, "Metro Agency Group", "Liberty", 400.0, "POL-999999", "2026-03-01", 9.0]
    result = one([HEADER] + records(3) + [row])
    assert len(result.frame) == 4
    assert "Total Risk PC" in result.frame["profitcentername"].to_list()


def test_footers_are_removed_whatever_language_they_are_in():
    rows = [HEADER] + records(4) + [["*** Ende des Berichts ***"] + [None] * 7]
    result = one(rows)
    assert len(result.frame) == 4
    assert rowclass.FOOTER in kinds(result)


# --------------------------------------------------------------------------- #
# Orientation and pivots
# --------------------------------------------------------------------------- #


def test_a_transposed_table_is_flipped():
    upright = [HEADER] + records(6)
    transposed = [list(column) for column in zip(*upright)]
    result = one(transposed)
    assert list(result.frame.columns) == NAMES
    assert len(result.frame) == 6


def test_a_transposed_table_with_an_internal_blank_row_stays_one_table():
    upright = [HEADER] + records(6)
    transposed = [list(column) for column in zip(*upright)]
    with_gap = transposed[:4] + [[None] * len(transposed[0])] + transposed[4:]
    result = one(with_gap)
    assert len(result.frame) == 6
    assert list(result.frame.columns) == NAMES


def test_a_pivot_matrix_is_unpivoted_rather_than_read_sideways():
    header = ["Producer", "Jan-2026", "Feb-2026", "Mar-2026", "Apr-2026"]
    rows = [header] + [
        [agency, 100.0 + index, 200.0 + index, 300.0 + index, 400.0 + index]
        for index, agency in enumerate(["Pinnacle", "Apex", "Metro", "Coastal"])
    ]
    result = one(rows)
    assert list(result.frame.columns) == ["producer", "period", "value"]
    # Every cell of the matrix becomes exactly one row.
    assert len(result.frame) == 4 * 4
    assert set(result.frame["period"]) == {"Jan-2026", "Feb-2026", "Mar-2026", "Apr-2026"}
    assert result.trace["pivot"]["unpivoted"] is True


def test_an_ordinary_table_is_never_unpivoted():
    result = one([HEADER] + records(6))
    assert result.trace.get("pivot") is None
    assert list(result.frame.columns) == NAMES


# --------------------------------------------------------------------------- #
# Types and reporting
# --------------------------------------------------------------------------- #


def test_mixed_date_formats_are_normalised_and_the_invalid_one_reported():
    formats = ["01/01/2026", "2026-01-08", "Jan 22 2026", "2026/01/29", "not a date", "02-12-2026"]
    rows = [HEADER] + [record(index)[:6] + [formats[index]] + [5.0] for index in range(6)]
    result = one(rows)
    column = result.frame["accountingeffectivedate"]
    assert column.dtype == pl.Date
    dates = column.cast(pl.String).to_list()
    assert dates[:2] == ["2026-01-01", "2026-01-08"]
    assert dates[4] is None
    assert any(f["reason"] == "unparseable date" for f in result.trace["coercion_failures"])


def test_leading_zero_identifiers_stay_text():
    rows = [HEADER] + [
        record(index)[:1] + [f"{1005 + index:06d}"] + record(index)[2:] for index in range(5)
    ]
    result = one(rows)
    assert result.frame["profitcenternumber"][0].startswith("0")


def test_constant_width_codes_stay_text_rather_than_becoming_numbers():
    result = one([HEADER] + records(6))
    assert result.frame["profitcenternumber"].dtype == pl.String


def test_duplicate_keys_are_flagged_not_deduplicated():
    rows = [HEADER] + records(3) + records(3)
    result = one(rows)
    assert len(result.frame) == 6
    assert "duplicate_key_within_region" in {f["check"] for f in result.trace["validation"]}


def test_unrecognised_columns_are_kept():
    rows = [HEADER + ["Underwriter Notes"]] + [row + ["renewal"] for row in records(5)]
    result = one(rows)
    assert "underwriter_notes" in result.frame.columns


def test_unparseable_values_are_reported_and_the_row_survives():
    bad = record(5)[:4] + ["n/a", "POL-999999", "not a date", 6.0]
    result = one([HEADER] + records(5) + [bad])
    reasons = {f["reason"] for f in result.trace["coercion_failures"]}
    assert reasons == {"not numeric", "unparseable date"}
    assert len(result.frame) == 6
    assert result.frame["premium"][5] is None
    assert result.frame["policynumber"][5] == "POL-999999"


def test_an_empty_sheet_is_handled():
    result = one([[None, None], [None, None]])
    assert result.frame.is_empty()
    assert "no table-shaped region" in result.trace["notes"][0]


def test_every_dropped_row_carries_a_reason_and_a_sheet_row():
    rows = [["Banner"] + [None] * 7, HEADER] + records(3) + [["Page 1 of 2"] + [None] * 7]
    result = one(rows)
    for dropped in result.trace["dropped_rows"]:
        assert dropped["reason"]
        assert dropped["sheet_row"] is not None


def test_identifiers_are_never_stripped_into_numbers():
    """'POL-1' once became -1: the numeric cleaner deleted the letters.

    The result was a perfectly valid number, so nothing reported it. Only a corpus whose
    policy numbers were all six digits long kept it hidden.
    """
    from ahi_clean import coerce

    for values in (["POL-1", "POL-2", "POL-3"], ["INV-7", "INV-8", "INV-9"]):
        result = coerce.coerce_column("reference", values)
        assert result.series.to_list() == values, result.series.to_list()


def test_presentation_is_still_stripped_from_genuine_numbers():
    from ahi_clean import coerce

    result = coerce.coerce_column("amount", ["$1,234.56", "(1,000.00)", "2 500", "99%"])
    assert result.series.to_list() == [1234.56, -1000.0, 2500.0, 99.0]


# --------------------------------------------------------------------------- #
# Date normalisation to YYYY-MM-DD
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-08 10:30:00",  # yyyy-MM-dd HH:mm:ss
        "2026-01-08",  # yyyy-MM-dd
        "01/08/2026",  # MM/dd/yyyy
        "1/8/2026",  # M/d/yyyy
        "20260108",  # yyyyMMdd
        "01-08-2026",  # MM-dd-yyyy
        "1-8-2026",  # M-d-yyyy
        "46030",  # Excel serial, in a column that already holds dates
    ],
)
def test_every_supported_date_format_is_written_as_iso(value):
    from ahi_clean import coerce

    result = coerce.coerce_column("when", ["2026-02-01", "03/15/2026", value])
    assert result.kind == "date"
    assert result.series.dtype == pl.Date
    assert result.series.cast(pl.String).to_list()[-1] == "2026-01-08"
    assert not result.failures


def test_a_mixed_format_column_normalises_every_value():
    from ahi_clean import coerce

    values = ["2026-01-08 10:30:00", "01/09/2026", "1-10-2026", 20260111, 46035.0, None]
    result = coerce.coerce_column("when", values)
    assert result.series.cast(pl.String).to_list() == [
        "2026-01-08", "2026-01-09", "2026-01-10", "2026-01-11", "2026-01-13", None,
    ]


def test_a_column_of_compact_yyyymmdd_numbers_is_a_date_column():
    from ahi_clean import coerce

    result = coerce.coerce_column("when", [20260108, 20260109, 20251231, 20240229])
    assert result.series.cast(pl.String).to_list() == ["2026-01-08", "2026-01-09", "2025-12-31", "2024-02-29"]


def test_eight_digit_identifiers_are_not_read_as_dates():
    """One value that is no calendar date and a numeric column is an identifier."""
    from ahi_clean import coerce

    for values in ([10050001, 10050002, 10050003], [20260108, 20261399, 20260110]):
        result = coerce.coerce_column("account", values)
        assert result.kind != "date", values


def test_a_column_of_only_five_digit_numbers_is_not_read_as_serial_dates():
    """ZIPs and codes look exactly like serials; nothing in the column says 'date'."""
    from ahi_clean import coerce

    result = coerce.coerce_column("zip", [75202, 46030, 90210, 10001])
    assert result.kind != "date"
    assert result.series.to_list() == ["75202", "46030", "90210", "10001"]

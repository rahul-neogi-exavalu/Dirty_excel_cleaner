"""Header detection and naming, including every degenerate header shape."""

import pytest

from ahi_clean.header import build_names, find_header, normalize_label

HEADER = ["Name", "Num", "Amount", "Date"]


def records(count=5):
    return [
        [f"PC{index}", 1000 + index, 100.0 + index, f"2026-01-{index + 1:02d}"]
        for index in range(count)
    ]


def detect(rows):
    width = max(len(row) for row in rows)
    padded = [list(row) + [None] * (width - len(row)) for row in rows]
    return find_header(padded, width)


# --------------------------------------------------------------------------- #
# Locating the header
# --------------------------------------------------------------------------- #


def test_clean_header_is_found():
    result = detect([HEADER] + records())
    assert (result.detected, result.row_index) == (True, 0)
    assert result.score > 0.85


def test_header_with_blank_cells_is_still_found():
    result = detect([["Name", None, "Amount", None]] + records())
    assert (result.detected, result.row_index) == (True, 0)


def test_header_with_empty_string_cells_is_still_found():
    result = detect([["Name", "", "Amount", ""]] + records())
    assert (result.detected, result.row_index) == (True, 0)


def test_header_with_duplicate_labels_is_still_found():
    result = detect([["Name", "Num", "Name", "Date"]] + records())
    assert (result.detected, result.row_index) == (True, 0)


def test_header_below_banners_is_found():
    rows = [
        ["Acme Corp", None, None, None],
        [None, None, None, None],
        ["Generated 2026", None, None, None],
        HEADER,
    ] + records()
    result = detect(rows)
    assert (result.detected, result.row_index) == (True, 3)


# --------------------------------------------------------------------------- #
# Refusing to invent a header
# --------------------------------------------------------------------------- #


def test_an_entirely_blank_header_row_is_refused():
    result = detect([[None, None, None, None]] + records())
    assert result.detected is False
    assert result.row_index is None
    assert result.names == ["column_1", "column_2", "column_3", "column_4"]
    assert "named positionally" in result.notes[0]


def test_data_starting_immediately_is_reported_headerless():
    """Naming a data row as the header is worse than admitting there is none."""
    result = detect(records(6))
    assert result.detected is False
    assert result.names[0] == "column_1"


def test_headerless_result_still_names_every_column():
    result = detect(records(6))
    assert len(result.names) == 4
    assert len(set(result.names)) == 4


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Producer/AgencyName", "producer_agencyname"),
        ("Profit Center Name", "profit_center_name"),
        ("Commission%", "commission"),
        ("  Spaced  Out  ", "spaced_out"),
        ("2024", "col_2024"),
        ("---", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_label_normalisation(label, expected):
    assert normalize_label(label) == expected


def test_blank_labels_become_positional_names():
    names = build_names(["Name", None, "Amount", ""])
    assert names == ["name", "column_2", "amount", "column_4"]


def test_labels_that_normalise_to_nothing_become_positional():
    assert build_names(["Name", "---", "***"]) == ["name", "column_2", "column_3"]


def test_duplicate_labels_are_suffixed_in_order():
    assert build_names(["Name", "Num", "Name", "Name"]) == ["name", "num", "name_2", "name_3"]


def test_duplicates_that_differ_only_in_punctuation_are_caught():
    assert build_names(["Net Amount", "net_amount"]) == ["net_amount", "net_amount_2"]


def test_numeric_headers_never_produce_bare_digit_names():
    names = build_names([2023, 2024, 2025])
    assert names == ["col_2023", "col_2024", "col_2025"]
    assert not any(name.isdigit() for name in names)


def test_every_name_is_unique_even_when_all_labels_are_blank():
    names = build_names([None, None, None])
    assert names == ["column_1", "column_2", "column_3"]


# --------------------------------------------------------------------------- #
# Multi-row headers
# --------------------------------------------------------------------------- #


def test_two_row_header_is_merged():
    rows = [
        ["Policy", "Premium", "Premium", "Date"],
        ["Number", "Gross", "Net", None],
    ] + records()
    result = detect(rows)
    assert result.multi_row is True
    assert result.names[0] == "policy_number"
    assert result.names[1] == "premium_gross"
    # The third column keeps its own identity rather than colliding with the second.
    assert result.names[2] == "premium_net"
    # A blank on the second row leaves the first row's label intact.
    assert result.names[3] == "date"


def test_a_single_row_header_is_not_merged_with_data():
    result = detect([HEADER] + records())
    assert result.multi_row is False
    assert result.names == ["name", "num", "amount", "date"]

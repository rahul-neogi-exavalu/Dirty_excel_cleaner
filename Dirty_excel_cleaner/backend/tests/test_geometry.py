"""Region segmentation: blank gaps of any size, anywhere, in both directions."""

import pytest

from ahi_clean.geometry import find_regions

HEADER = ["Name", "Num", "Amount", "Date"]


def records(count, start=0):
    return [
        [f"PC{index}", 1000 + index, 100.0 + index, f"2026-01-{index + 1:02d}"]
        for index in range(start, start + count)
    ]


def blanks(count, width=4):
    return [[None] * width for _ in range(count)]


# --------------------------------------------------------------------------- #
# Blank rows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gap", [1, 2, 3, 5, 12])
def test_blank_rows_of_any_length_do_not_split_one_table(gap):
    """Gap size is never an input. Only what follows the gap decides."""
    grid = [HEADER] + records(3) + blanks(gap) + records(3, start=3)
    [region] = find_regions(grid)
    assert region.height == 7  # header + 6 records, blanks absorbed


@pytest.mark.parametrize("gap", [1, 3])
def test_a_different_table_after_a_gap_is_a_separate_region(gap):
    grid = (
        [HEADER]
        + records(4)
        + blanks(gap)
        + [["Region", "Total", None, None]]
        + [["East", 500.0, None, None], ["West", 600.0, None, None]]
    )
    regions = find_regions(grid)
    assert len(regions) == 2
    assert regions[0].height == 5


def test_a_sparse_trailing_run_stays_with_its_table():
    """A footer must not be promoted to a table of its own."""
    grid = [HEADER] + records(4) + blanks(2) + [["*** End of Report ***", None, None, None]]
    [region] = find_regions(grid)
    assert region.height == 6


def test_leading_and_trailing_blank_rows_are_trimmed():
    grid = blanks(3) + [HEADER] + records(4) + blanks(4)
    [region] = find_regions(grid)
    assert region.height == 5
    assert region.row_offset == 3


def test_a_sheet_of_only_blanks_yields_nothing():
    assert find_regions(blanks(6)) == []


# --------------------------------------------------------------------------- #
# Blank columns
# --------------------------------------------------------------------------- #


def widen(rows, gap, at=2):
    """Insert `gap` empty columns into every row at position `at`."""
    return [row[:at] + [None] * gap + row[at:] for row in rows]


@pytest.mark.parametrize("gap", [1, 2, 3, 4, 7])
def test_empty_columns_of_any_width_are_a_gutter_not_a_split(gap):
    grid = widen([HEADER] + records(4), gap)
    [region] = find_regions(grid)
    # The gutter is removed, the four real columns survive as one table.
    assert region.width == 4
    assert region.height == 5


def test_leading_empty_columns_are_trimmed():
    grid = [[None, None] + row for row in [HEADER] + records(4)]
    [region] = find_regions(grid)
    assert region.width == 4
    assert region.column_offset == 2


def test_side_by_side_tables_with_different_extents_are_split():
    grid = [
        ["Name", "Num", None, None, "Region", "Total"],
        ["PC0", 1000, None, None, "East", 500.0],
        ["PC1", 1001, None, None, "West", 600.0],
        ["PC2", 1002, None, None, None, None],
        ["PC3", 1003, None, None, None, None],
    ]
    regions = find_regions(grid)
    assert len(regions) == 2
    assert [region.width for region in regions] == [2, 2]
    assert regions[1].column_offset == 4


def test_a_column_with_data_but_no_header_is_kept():
    """Only a column empty top to bottom is a gutter."""
    grid = [HEADER[:2] + [None] + HEADER[2:]] + [
        row[:2] + ["note"] + row[2:] for row in records(3)
    ]
    [region] = find_regions(grid)
    assert region.width == 5


# --------------------------------------------------------------------------- #
# Combined
# --------------------------------------------------------------------------- #


def test_stacked_and_side_by_side_together():
    top = [
        ["Name", "Num", None, "Region", "Total"],
        ["PC0", 1000, None, "East", 500.0],
        ["PC1", 1001, None, None, None],
        ["PC2", 1002, None, None, None],
    ]
    bottom = [["Note"], ["free text here"], ["more free text"]]
    grid = top + [[None] * 5] * 2 + [row + [None] * 4 for row in bottom]
    regions = find_regions(grid)
    assert len(regions) >= 2


def test_region_offsets_point_back_into_the_sheet():
    grid = blanks(2) + [[None] + row for row in [HEADER] + records(3)]
    [region] = find_regions(grid)
    assert (region.row_offset, region.column_offset) == (2, 1)


def test_a_multi_line_banner_block_is_not_a_table():
    """Title + timestamp + notice, fenced off by blanks, is still not tabular."""
    grid = (
        [["Acme Corporation", None, None, None]]
        + [["Report Generated 2026-01-01", None, None, None]]
        + [["Confidential", None, None, None]]
        + blanks(2)
        + [HEADER]
        + records(4)
    )
    [region] = find_regions(grid)
    assert region.height == 5
    assert region.rows[0][0] == "Name"

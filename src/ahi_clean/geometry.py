"""Split a sheet into table regions.

A sheet is not assumed to hold exactly one table starting at A1. It may hold several,
stacked with gaps or sitting side by side, surrounded by banners and footers.

The governing rule: **gap size is never an input to any decision.** One blank row and
twelve blank rows are treated identically. What decides is whether what sits on the
other side of the gap looks like the same table -- by column type profile for rows,
by row extent for columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import signals
from .typing_utils import is_blank

PROFILE_MATCH_THRESHOLD = 0.7
EXTENT_MATCH_THRESHOLD = 0.75
MIN_TABLE_ROWS = 2


@dataclass
class Region:
    """A rectangle of a sheet believed to hold one table.

    ``rows`` holds only populated rows -- interior blank runs are removed, since a
    decorative gap carries no data. ``source_rows`` keeps each row's original sheet
    index so the audit report can still point a reviewer at the right line.
    """

    rows: list[list]
    source_rows: list[int] = field(default_factory=list)
    column_offset: int = 0
    origin: str = "row_segmentation"

    @property
    def row_offset(self) -> int:
        return self.source_rows[0] if self.source_rows else 0

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


def find_regions(grid: list[list]) -> list[Region]:
    """Segment a raw sheet grid into table regions."""
    regions: list[Region] = []
    for row_region in _segment_rows(grid):
        regions.extend(_segment_columns(row_region))
    return [region for region in regions if _is_table(region)]


def _is_table(region: Region) -> bool:
    """Reject regions that cannot be a table however they are read.

    A block of single-cell lines is a banner or a footer that happened to be fenced off
    by blank rows -- a title, a timestamp and a confidentiality notice stacked together.
    It has no second column, so there is nothing to tabulate.
    """
    if region.height < MIN_TABLE_ROWS:
        return False
    return signals.modal_fill(region.rows) >= 2


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def _blank_row(row) -> bool:
    return all(is_blank(cell) for cell in row)


def _runs_of_populated_rows(grid) -> list[tuple[int, int]]:
    """Index ranges of consecutive non-blank rows."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(grid):
        if _blank_row(grid[index]):
            index += 1
            continue
        start = index
        while index < len(grid) and not _blank_row(grid[index]):
            index += 1
        runs.append((start, index))
    return runs


def _belongs_to_previous(anchor_rows, candidate_rows, width) -> bool:
    """Whether a run continues the region before it, across a gap of any size."""
    anchor_fill = signals.modal_fill(anchor_rows)
    candidate_fill = signals.modal_fill(candidate_rows)

    # A run of single-cell lines is a banner or a footer, never a table. Attaching it
    # here keeps it out of the region list and lets row classification judge it against
    # the real table's fill baseline. Note this tests for *one* populated cell, not
    # merely "fewer than the table" -- a genuine narrow table alongside a wide one is
    # sparse by that looser measure and must not be swallowed.
    if candidate_fill <= 1 < anchor_fill:
        return True

    similarity = signals.profile_similarity(
        signals.type_profile(anchor_rows, width),
        signals.type_profile(candidate_rows, width),
    )
    return similarity >= PROFILE_MATCH_THRESHOLD


def _segment_rows(grid: list[list]) -> list[Region]:
    """Group runs of populated rows into regions by column type profile."""
    runs = _runs_of_populated_rows(grid)
    if not runs:
        return []

    width = max((len(row) for row in grid), default=0)
    padded = [list(row) + [None] * (width - len(row)) for row in grid]

    groups: list[list[tuple[int, int]]] = [[runs[0]]]
    for run in runs[1:]:
        anchor_rows = [row for start, end in groups[-1] for row in padded[start:end]]
        if _belongs_to_previous(anchor_rows, padded[run[0] : run[1]], width):
            groups[-1].append(run)
        else:
            groups.append([run])

    regions = []
    for group in groups:
        indices = [index for start, end in group for index in range(start, end)]
        regions.append(
            Region(rows=[padded[index] for index in indices], source_rows=indices)
        )
    return regions


# --------------------------------------------------------------------------- #
# Columns
# --------------------------------------------------------------------------- #


def _runs_of_populated_columns(rows, width) -> list[tuple[int, int]]:
    populated = [
        any(index < len(row) and not is_blank(row[index]) for row in rows)
        for index in range(width)
    ]
    runs: list[tuple[int, int]] = []
    index = 0
    while index < width:
        if not populated[index]:
            index += 1
            continue
        start = index
        while index < width and populated[index]:
            index += 1
        runs.append((start, index))
    return runs


def _row_extent(rows, start, end) -> tuple[int, int] | None:
    """First and last row index in which a column range holds anything."""
    occupied = [
        index
        for index, row in enumerate(rows)
        if any(
            position < len(row) and not is_blank(row[position])
            for position in range(start, end)
        )
    ]
    return (occupied[0], occupied[-1]) if occupied else None


def _extents_match(left, right) -> bool:
    """Whether two column runs span the same rows, within a tolerance."""
    if left is None or right is None:
        return False
    left_span = left[1] - left[0] + 1
    right_span = right[1] - right[0] + 1
    overlap = min(left[1], right[1]) - max(left[0], right[0]) + 1
    if overlap <= 0:
        return False
    return overlap / max(left_span, right_span) >= EXTENT_MATCH_THRESHOLD


def _segment_columns(region: Region) -> list[Region]:
    """Split a row region where blank columns separate genuinely different tables.

    Two column runs spanning the same rows are one table with a decorative gutter --
    File1's column F. Two runs spanning different rows are separate tables placed side
    by side, and are returned as separate regions.
    """
    width = region.width
    runs = _runs_of_populated_columns(region.rows, width)
    if not runs:
        return []

    groups: list[list[tuple[int, int]]] = [[runs[0]]]
    for run in runs[1:]:
        previous = groups[-1][-1]
        if _extents_match(
            _row_extent(region.rows, *previous), _row_extent(region.rows, *run)
        ):
            groups[-1].append(run)
        else:
            groups.append([run])

    regions = []
    for group in groups:
        # Within a group the gutter columns are dropped; across groups the split stands.
        keep = [index for start, end in group for index in range(start, end)]
        rows, sources = [], []
        for row, source in zip(region.rows, region.source_rows):
            sliced = [row[index] if index < len(row) else None for index in keep]
            if _blank_row(sliced):
                # This row belonged to the neighbouring table, not this one.
                continue
            rows.append(sliced)
            sources.append(source)
        if not rows:
            continue
        regions.append(
            Region(
                rows=rows,
                source_rows=sources,
                column_offset=region.column_offset + group[0][0],
                origin="column_segmentation" if len(groups) > 1 else region.origin,
            )
        )
    return regions

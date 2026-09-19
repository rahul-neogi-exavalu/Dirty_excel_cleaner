"""Read a workbook into a plain grid of Python values, one grid per sheet.

Everything downstream works on lists of lists rather than openpyxl objects, so
the structural heuristics stay easy to test with hand-written fixtures.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field

import openpyxl


@dataclass
class SheetGrid:
    """A sheet's cells as a dense rectangular grid, plus what we noticed reading it."""

    name: str
    rows: list[list]
    error_cells: list[str] = field(default_factory=list)
    merged_ranges: list[str] = field(default_factory=list)
    image_count: int = 0

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


def read_workbook(path) -> list[SheetGrid]:
    """Load every sheet of ``path`` into a :class:`SheetGrid`."""
    # data_only=True gives us cached formula results rather than formula text.
    workbook = openpyxl.load_workbook(path, data_only=True)
    return [_read_sheet(worksheet) for worksheet in workbook.worksheets]


def count_embedded_images(path) -> int:
    """Count images in the drawing layer.

    Embedded logos are anchored to drawings, not to cells, so they are invisible
    to any value-based scan -- File2's banner occupies rows that read as empty.
    They never affect extraction; this is recorded for the audit log so a reviewer
    can see why the top of that sheet looked blank.
    """
    with zipfile.ZipFile(path) as archive:
        return sum(1 for name in archive.namelist() if name.startswith("xl/media/"))


def _read_sheet(worksheet) -> SheetGrid:
    height = worksheet.max_row or 0
    width = worksheet.max_column or 0
    rows: list[list] = [[None] * width for _ in range(height)]
    error_cells: list[str] = []

    for row_index in range(1, height + 1):
        for column_index in range(1, width + 1):
            cell = worksheet.cell(row_index, column_index)
            value = cell.value
            # Excel error cells ('#VALUE!', '#REF!', ...) come back as ordinary
            # strings. Left alone they read as populated and defeat blank-row
            # detection -- File2!A1 sits under the logo and would anchor the
            # header hunt to row 1. Null them here, once, before anything scores.
            if cell.data_type == "e":
                error_cells.append(f"{cell.coordinate}={value}")
                value = None
            if isinstance(value, str):
                value = value.strip() or None
            rows[row_index - 1][column_index - 1] = value

    _propagate_merged_values(worksheet, rows)

    return SheetGrid(
        name=worksheet.title,
        rows=rows,
        error_cells=error_cells,
        merged_ranges=[str(merged) for merged in worksheet.merged_cells.ranges],
        image_count=len(getattr(worksheet, "_images", [])),
    )


def _propagate_merged_values(worksheet, rows: list[list]) -> None:
    """Fill a merged range with its top-left value so the grid stays rectangular."""
    for merged in worksheet.merged_cells.ranges:
        top_left = rows[merged.min_row - 1][merged.min_col - 1]
        if top_left is None:
            continue
        for row_index in range(merged.min_row, merged.max_row + 1):
            for column_index in range(merged.min_col, merged.max_col + 1):
                rows[row_index - 1][column_index - 1] = top_left

"""Read a workbook into a plain grid of Python values, one grid per sheet.

Everything downstream works on lists of lists rather than openpyxl objects, so
the structural heuristics stay easy to test with hand-written fixtures.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from . import delimited


@dataclass
class SheetGrid:
    """A sheet's cells as a dense rectangular grid, plus what we noticed reading it."""

    name: str
    rows: list[list]
    styles: list[list] = field(default_factory=list)
    error_cells: list[str] = field(default_factory=list)
    formula_cells: list[str] = field(default_factory=list)
    uncached_formula_cells: list[str] = field(default_factory=list)
    merged_ranges: list[str] = field(default_factory=list)
    image_count: int = 0
    source_format: str = "xlsx"
    # Flags about how the file itself was read (separator, encoding); every column of
    # every table from this grid carries them. See flags.py.
    read_flags: list[str] = field(default_factory=list)

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


# A sheet beyond this many cells is refused rather than attempted. A documented
# ceiling is a feature; an unexplained hang that swaps the machine is not.
DEFAULT_MAX_CELLS = 20_000_000


class SheetTooLarge(Exception):
    """A sheet exceeds the configured cell ceiling."""


DELIMITED_SUFFIXES = {".csv", ".tsv", ".txt"}


def read_workbook(path, max_cells: int = DEFAULT_MAX_CELLS) -> list[SheetGrid]:
    """Load every sheet of ``path`` into a :class:`SheetGrid`.

    A delimited file is one sheet by definition and takes a different reader, but yields
    the same grid -- which is the point of the grid being a list of lists. Nothing
    downstream needs to know where the cells came from.
    """
    if Path(path).suffix.lower() in DELIMITED_SUFFIXES:
        return delimited.read_delimited(path, max_cells, SheetTooLarge)

    # data_only=True gives us cached formula results rather than formula text.
    workbook = openpyxl.load_workbook(path, data_only=True)
    # A second, cheap pass over the formula text. data_only gives the value Excel
    # cached the last time it saved; a workbook written by a library and never opened
    # in Excel has no cache, so a formula-driven column arrives silently empty. Knowing
    # which cells hold formulas is the difference between reporting that and shipping
    # a column of nulls as if the source were blank.
    formulas = openpyxl.load_workbook(path, data_only=False)
    grids = []
    for worksheet in workbook.worksheets:
        grid = _read_sheet(worksheet, max_cells)
        _note_formulas(grid, formulas[worksheet.title])
        grids.append(grid)
    return grids


def _populated_extent(worksheet) -> tuple[int, int]:
    """The genuinely used rectangle, not the one Excel claims.

    openpyxl reports the sheet's declared dimensions, which stray formatting inflates
    freely -- a single styled cell at row 100,000 makes a ten-row report claim a hundred
    thousand rows, and materialising that dense grid costs memory the data never needed.
    Trailing empty rows and columns are walked back before anything is allocated.
    """
    height = worksheet.max_row or 0
    width = worksheet.max_column or 0

    while height > 0 and _row_is_empty(worksheet, height, width):
        height -= 1
    while width > 0 and _column_is_empty(worksheet, width, height):
        width -= 1
    return height, width


def _row_is_empty(worksheet, row, width) -> bool:
    return all(worksheet.cell(row, column).value is None for column in range(1, width + 1))


def _column_is_empty(worksheet, column, height) -> bool:
    return all(worksheet.cell(row, column).value is None for row in range(1, height + 1))


def _note_formulas(grid: SheetGrid, worksheet) -> None:
    for row in worksheet.iter_rows():
        for cell in row:
            if cell.data_type != "f":
                continue
            grid.formula_cells.append(cell.coordinate)
            value = grid.rows[cell.row - 1][cell.column - 1] if cell.row <= grid.height else None
            if value is None:
                grid.uncached_formula_cells.append(cell.coordinate)


def count_embedded_images(path) -> int:
    """Count images in the drawing layer.

    Embedded logos are anchored to drawings, not to cells, so they are invisible
    to any value-based scan -- File2's banner occupies rows that read as empty.
    They never affect extraction; this is recorded for the audit log so a reviewer
    can see why the top of that sheet looked blank.
    """
    # A delimited file is not a zip and cannot carry a drawing layer at all.
    if Path(path).suffix.lower() in DELIMITED_SUFFIXES:
        return 0
    with zipfile.ZipFile(path) as archive:
        return sum(1 for name in archive.namelist() if name.startswith("xl/media/"))


def _read_sheet(worksheet, max_cells: int = DEFAULT_MAX_CELLS) -> SheetGrid:
    height, width = _populated_extent(worksheet)
    if height * width > max_cells:
        raise SheetTooLarge(
            f"sheet '{worksheet.title}' spans {height} x {width} = {height * width:,} cells, "
            f"above the {max_cells:,} ceiling"
        )
    rows: list[list] = [[None] * width for _ in range(height)]
    styles: list[list] = [[None] * width for _ in range(height)]
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
            if value is not None:
                styles[row_index - 1][column_index - 1] = _style_of(cell)

    _propagate_merged_values(worksheet, rows)

    return SheetGrid(
        name=worksheet.title,
        rows=rows,
        styles=styles,
        error_cells=error_cells,
        merged_ranges=[str(merged) for merged in worksheet.merged_cells.ranges],
        image_count=len(getattr(worksheet, "_images", [])),
    )


def _style_of(cell) -> dict:
    """Visual emphasis on a cell, used as one header-detection signal.

    Report writers style header rows and leave data rows plain, so this is free
    evidence already sitting in the file. It is only ever additive: a workbook with no
    formatting scores zero here and is decided by the content signals alone.
    """
    font = cell.font
    fill = cell.fill
    filled = bool(
        fill is not None
        and fill.fill_type not in (None, "none")
        and getattr(fill.fgColor, "rgb", None) not in (None, "00000000")
    )
    bordered = bool(cell.border is not None and cell.border.bottom is not None
                    and cell.border.bottom.style)
    return {
        "bold": bool(font is not None and font.bold),
        "filled": filled,
        "bordered": bordered,
        "emphasised": bool((font is not None and font.bold) or filled or bordered),
    }


def _propagate_merged_values(worksheet, rows: list[list]) -> None:
    """Fill a merged range with its top-left value so the grid stays rectangular."""
    for merged in worksheet.merged_cells.ranges:
        top_left = rows[merged.min_row - 1][merged.min_col - 1]
        if top_left is None:
            continue
        for row_index in range(merged.min_row, merged.max_row + 1):
            for column_index in range(merged.min_col, merged.max_col + 1):
                rows[row_index - 1][column_index - 1] = top_left

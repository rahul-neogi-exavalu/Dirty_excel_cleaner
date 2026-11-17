"""Read a legacy Excel 97-2003 workbook (.xls) into the same grids the xlsx reader produces.

The structure downstream only ever sees a :class:`~ahi_clean.reader.SheetGrid`, so this
module's whole job is to give it the same values openpyxl would have: text stripped and
blank as ``None``, error cells nulled and recorded, dates as ``datetime``, merged ranges
filled from their top-left cell, the sheet trimmed to the rectangle that holds something.

What an .xls cannot say, and what follows from it:

* **No formula information.** The format stores the last calculated value only, so
  ``formula_cells`` stays empty and "formula with no saved result" cannot be reported.
* **Numbers are always floats.** A whole number is returned as an int, the way an .xlsx
  stores one, so the type lattice classifies both formats alike.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import xlrd
from openpyxl.utils import get_column_letter

from .reader import SheetGrid, _fit, _propagate_merged_values

# Whole floats above this stay floats: they are no longer exactly representable as ints.
_MAX_EXACT_INT = 2 ** 53


def _open(path, on_demand: bool = True) -> xlrd.Book:
    """Open the workbook, keeping cell formatting when the file's BIFF version allows it."""
    try:
        return xlrd.open_workbook(str(path), formatting_info=True, on_demand=on_demand)
    except NotImplementedError:
        # formatting_info is not implemented for BIFF < 8; the values are all that is lost.
        return xlrd.open_workbook(str(path), on_demand=on_demand)


def sheet_names(path) -> list[str]:
    book = _open(path)
    try:
        return book.sheet_names()
    finally:
        book.release_resources()


def inspect_sheets(path) -> list[dict]:
    """``{name, hidden, has_content}`` for every sheet, for the upload step."""
    book = _open(path)
    try:
        found = []
        for index, name in enumerate(book.sheet_names()):
            sheet = book.sheet_by_index(index)
            found.append({
                "name": name,
                "hidden": sheet.visibility != 0,
                "has_content": _has_content(sheet),
            })
            book.unload_sheet(index)
        return found
    finally:
        book.release_resources()


def _has_content(sheet) -> bool:
    for row in range(sheet.nrows):
        for kind, value in zip(sheet.row_types(row), sheet.row_values(row)):
            if kind not in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) and str(value).strip() != "":
                return True
    return False


def read_sheets(path, max_cells: int, sheet_too_large, only=None) -> list[SheetGrid]:
    """Every sheet of ``path``, or only those named in ``only``, in workbook order."""
    book = _open(path)
    try:
        grids = []
        for index, name in enumerate(book.sheet_names()):
            if only is not None and name not in only:
                continue
            grids.append(_read_sheet(book, book.sheet_by_index(index), max_cells, sheet_too_large))
            book.unload_sheet(index)
        return grids
    finally:
        book.release_resources()


def raw_rows(path, name: str):
    """One sheet's values as the file stores them, for showing the file as it is.

    Returns ``(rows, merged)`` like ``reader.raw_sheet``: value tuples from row 1 on, error
    cells keeping their ``#VALUE!`` text, merged ranges as ``(min_col, min_row, max_col,
    max_row)`` (1-based, inclusive). Raises ``KeyError`` for a sheet the file does not have.
    """
    book = _open(path)
    try:
        if name not in book.sheet_names():
            raise KeyError(name)
        sheet = book.sheet_by_name(name)
        rows = [
            tuple(_value(book, sheet.cell(row, column), keep_errors=True) for column in range(sheet.ncols))
            for row in range(sheet.nrows)
        ]
        merged = [(clo + 1, rlo + 1, chi, rhi) for rlo, rhi, clo, chi in sheet.merged_cells]
        return rows, merged
    finally:
        book.release_resources()


def _read_sheet(book, sheet, max_cells: int, sheet_too_large) -> SheetGrid:
    if sheet.nrows * sheet.ncols > max_cells:
        raise sheet_too_large(
            f"sheet '{sheet.name}' spans at least {sheet.nrows} x {sheet.ncols} = "
            f"{sheet.nrows * sheet.ncols:,} cells, above the {max_cells:,} ceiling"
        )
    raw_rows_: list[list] = []
    raw_styles: list[list] = []
    error_cells: list[str] = []
    style_cache: dict = {}
    height = width = 0

    for row in range(sheet.nrows):
        values: list = []
        styles: list = []
        for column in range(sheet.ncols):
            cell = sheet.cell(row, column)
            if cell.ctype == xlrd.XL_CELL_ERROR:
                # Same reasoning as the xlsx reader: an error cell must not read as populated.
                error_cells.append(
                    f"{get_column_letter(column + 1)}{row + 1}={xlrd.error_text_from_code.get(cell.value, '#VALUE!')}"
                )
                height, width = row + 1, max(width, column + 1)
                values.append(None)
                styles.append(None)
                continue
            value = _value(book, cell)
            if cell.ctype not in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK) and value not in (None, ""):
                height, width = row + 1, max(width, column + 1)
            if isinstance(value, str):
                value = value.strip() or None
            values.append(value)
            styles.append(None if value is None else _style_of(book, sheet, row, column, style_cache))
        raw_rows_.append(values)
        raw_styles.append(styles)

    del raw_rows_[height:], raw_styles[height:]
    rows = [_fit(values, width) for values in raw_rows_]
    styles = [_fit(values, width) for values in raw_styles]
    merged = [
        f"{get_column_letter(clo + 1)}{rlo + 1}:{get_column_letter(chi)}{rhi}"
        for rlo, rhi, clo, chi in sheet.merged_cells
    ]
    _propagate_merged_values(merged, rows)
    return SheetGrid(
        name=sheet.name,
        rows=rows,
        styles=styles,
        error_cells=error_cells,
        merged_ranges=merged,
        source_format="xls",
    )


def _value(book, cell, keep_errors: bool = False):
    kind, value = cell.ctype, cell.value
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if kind == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(value, "#VALUE!") if keep_errors else None
    if kind == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if kind == xlrd.XL_CELL_DATE:
        return _date(book, value)
    if kind == xlrd.XL_CELL_NUMBER:
        if float(value).is_integer() and abs(value) < _MAX_EXACT_INT:
            return int(value)
        return value
    return value


def _date(book, serial: float):
    """A date cell as ``datetime`` (or ``time`` for a time of day), honouring the 1904 system."""
    try:
        if 0 <= serial < 1:
            hour, minute, second = xlrd.xldate_as_tuple(serial, book.datemode)[3:]
            return datetime.time(hour, minute, second)
        return xlrd.xldate_as_datetime(serial, book.datemode)
    except (xlrd.XLDateError, ValueError, OverflowError):
        return serial


def _style_of(book, sheet, row: int, column: int, cache: dict):
    """Visual emphasis on a cell, in the dict shape the xlsx reader produces."""
    if not getattr(book, "formatting_info", False):
        return None
    index = sheet.cell_xf_index(row, column)
    found = cache.get(index)
    if found is None:
        found = cache[index] = _emphasis(book, index)
    return found


def _emphasis(book, xf_index: int) -> dict:
    xf = book.xf_list[xf_index]
    font = book.font_list[xf.font_index]
    bold = bool(font.bold)
    filled = xf.background.fill_pattern not in (0, None)
    bordered = bool(xf.border.bottom_line_style)
    return {"bold": bold, "filled": filled, "bordered": bordered,
            "emphasised": bold or filled or bordered}

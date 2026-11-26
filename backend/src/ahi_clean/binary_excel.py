"""Read a binary Excel workbook (.xlsb) into the same grids the xlsx reader produces.

openpyxl cannot open an .xlsb and xlrd stops at .xls, so this one goes through
python-calamine (a Rust reader, which is also why a 115,000-row sheet opens in two seconds).
It hands back values only, so what an .xlsb carries beyond them is not seen here:

* **No formatting.** ``styles`` is empty and the header-emphasis signal scores zero, exactly
  as for a CSV; the content signals decide.
* **No merged ranges or formula cells.** A merged banner arrives as one value followed by
  empties, which the banner rule already handles.

Everything else is shaped to match the xlsx reader: text stripped and blank as ``None``,
Excel error values nulled and recorded, whole numbers as ints, dates as ``datetime``.
"""

from __future__ import annotations

import datetime

from openpyxl.utils import get_column_letter

from .delimited import ERROR_TEXT
from .reader import SheetGrid, _fit

# Whole floats above this stay floats: they are no longer exactly representable as ints.
_MAX_EXACT_INT = 2 ** 53


def _open(path):
    from python_calamine import CalamineWorkbook

    return CalamineWorkbook.from_path(str(path))


def _is_worksheet(meta) -> bool:
    return "WorkSheet" in str(meta.typ)


def sheet_names(path) -> list[str]:
    return [meta.name for meta in _open(path).sheets_metadata if _is_worksheet(meta)]


def inspect_sheets(path) -> list[dict]:
    """``{name, hidden, has_content}`` for every worksheet, for the upload step."""
    book = _open(path)
    found = []
    for meta in book.sheets_metadata:
        if not _is_worksheet(meta):
            continue
        sheet = book.get_sheet_by_name(meta.name)
        found.append({
            "name": meta.name,
            "hidden": "Visible" not in str(meta.visible),
            "has_content": any(
                value not in ("", None) and str(value).strip() != ""
                for row in sheet.iter_rows() for value in row
            ),
        })
    return found


def raw_rows(path, name: str) -> list[tuple]:
    """One sheet's values as the file stores them, for showing the file as it is.

    Rows run from row 1 so each keeps its sheet position; nothing is stripped or nulled
    except the empty string calamine uses for an empty cell. Raises ``KeyError`` for a sheet
    the file does not have.
    """
    book = _open(path)
    if name not in sheet_names(path):
        raise KeyError(name)
    rows = book.get_sheet_by_name(name).to_python(skip_empty_area=False)
    return [tuple(None if value == "" else value for value in row) for row in rows]


def read_sheets(path, max_cells: int, sheet_too_large, only=None) -> list[SheetGrid]:
    """Every worksheet of ``path``, or only those named in ``only``, in workbook order."""
    book = _open(path)
    grids = []
    for meta in book.sheets_metadata:
        if not _is_worksheet(meta) or (only is not None and meta.name not in only):
            continue
        sheet = book.get_sheet_by_name(meta.name)
        if sheet.height * sheet.width > max_cells:
            raise sheet_too_large(
                f"sheet '{meta.name}' spans {sheet.height} x {sheet.width} = "
                f"{sheet.height * sheet.width:,} cells, above the {max_cells:,} ceiling"
            )
        grids.append(_grid(meta.name, sheet.to_python(skip_empty_area=False)))
    return grids


def _grid(name: str, raw_rows_) -> SheetGrid:
    rows: list[list] = []
    error_cells: list[str] = []
    height = width = 0
    for row_index, raw in enumerate(raw_rows_):
        values = []
        for column_index, cell in enumerate(raw):
            if isinstance(cell, str) and cell.strip() in ERROR_TEXT:
                # Same reasoning as the xlsx reader: an error cell must not read as empty
                # or as populated text, so it is nulled and accounted for separately.
                error_cells.append(f"{get_column_letter(column_index + 1)}{row_index + 1}={cell.strip()}")
                height, width = row_index + 1, max(width, column_index + 1)
                values.append(None)
                continue
            value = _value(cell)
            if value is not None:
                height, width = row_index + 1, max(width, column_index + 1)
            values.append(value)
        rows.append(values)
    del rows[height:]
    rows = [_fit(values, width) for values in rows]
    grid = SheetGrid(name=name, rows=rows, error_cells=error_cells, source_format="xlsb")
    grid.styles = [[None] * width for _ in rows]
    return grid


def _value(cell):
    if cell is None or cell == "":
        return None
    if isinstance(cell, str):
        return cell.strip() or None
    if isinstance(cell, bool):
        return cell
    if isinstance(cell, float):
        return int(cell) if cell.is_integer() and abs(cell) < _MAX_EXACT_INT else cell
    if isinstance(cell, datetime.datetime):
        return cell
    if isinstance(cell, datetime.date):
        return datetime.datetime(cell.year, cell.month, cell.day)
    if isinstance(cell, datetime.timedelta):
        return str(cell)
    return cell

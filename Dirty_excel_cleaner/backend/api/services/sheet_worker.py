"""The unit of parallel work: read one sheet and clean it.

Runs inside a worker process, so it imports only the cleaner -- nothing from the web
framework. Reading and cleaning happen together because reading is the larger half of
the cost (openpyxl's XML parse) and each sheet can be read without touching the others.
"""

from __future__ import annotations

from ahi_clean.extract import extract_sheet
from ahi_clean.reader import DEFAULT_MAX_CELLS, read_sheet


def process_sheet(path: str, name: str, reuse: bool = False, max_cells: int = DEFAULT_MAX_CELLS):
    """(grid, results) for one sheet. The grid goes back for the sheet-level accounting;
    its per-cell styles only served header detection here, so they are not sent back.

    ``reuse`` is set in worker processes: a worker usually gets several sheets of the same
    file, and the file's shared strings and styles need parsing only once."""
    grid = read_sheet(path, name, max_cells, reuse=reuse)
    results = extract_sheet(grid)
    grid.styles = []
    return grid, results


def warm() -> None:
    """Pay a worker's start-up imports before the first real sheet arrives."""
    import openpyxl  # noqa: F401
    import polars  # noqa: F401

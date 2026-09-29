"""Read a delimited text file into the same grid the Excel reader produces.

A CSV is one sheet by definition -- there is no second tab to find -- so this yields a
single grid and everything downstream is unchanged. That is the whole point of the grid
being a list of lists: the structural reasoning never knew where the cells came from.

What a CSV cannot carry, and what follows from it:

* **No cell types.** Every value arrives as text. This costs less than it sounds, because
  the type lattice already classifies by *shape* -- ``1005`` reads as an integer,
  ``POL-200001`` as an identifier, ``2026-01-01`` as a date -- and the coercion ladder
  parses strings anyway.
* **No formatting.** The emphasis signal scores zero, which it is designed to tolerate;
  plenty of .xlsx files carry no styling either.
* **No merged cells, formulas or error cells** as metadata. A merged title arrives as one
  value followed by empties, which is exactly what the banner rule already looks for, and
  an error written into the text is handled below.

One thing a CSV does *better*: a leading zero survives. ``08085`` is text in the file and
stays text, where Excel had already destroyed it before the pipeline ever saw the sheet.
"""

from __future__ import annotations

import csv
from pathlib import Path

# Tried in order against a sample of the file. Semicolon is the default in much of
# Europe, where the comma is the decimal separator, so a comma-only reader mangles those
# files into a single column without complaining.
DELIMITERS = [",", ";", "\t", "|"]

# utf-8-sig first so a byte-order mark does not end up glued to the first column name.
# The last is a single-byte codec that cannot fail, which guarantees a file is readable
# even when its declared or guessed encoding is wrong.
ENCODINGS = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]

# Excel writes these into a cell when a formula fails. In a workbook they arrive as typed
# error cells; in a CSV they are just text, and left alone they read as a populated cell
# and defeat blank-row detection exactly as they would have there.
ERROR_TEXT = {"#VALUE!", "#REF!", "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!"}

SNIFF_BYTES = 64_000


def read_text(path: Path) -> tuple[str, str]:
    """The file's text and the encoding that read it.

    Tried in order rather than sniffed, because a guess that fails loses the file while
    the last candidate is a single-byte codec that always succeeds. A file read with the
    wrong codec is a reviewable oddity; a file that would not open at all is not.
    """
    raw = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def sniff_delimiter(text: str) -> str:
    """Which character separates the fields.

    Decided by which candidate yields the most *consistent* column count across the
    opening lines, not by which is most frequent. Frequency picks the comma out of a
    semicolon-delimited file full of prose; consistency does not.
    """
    return sniff_delimiter_detail(text)[0]


def sniff_delimiter_detail(text: str) -> tuple[str, list[str]]:
    """The separator, and any other candidate that scored exactly as well."""
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if not lines:
        return ",", []
    scores = {}
    for delimiter in DELIMITERS:
        counts = [line.count(delimiter) for line in lines]
        if not any(counts):
            continue
        modal = max(set(counts), key=counts.count)
        if modal == 0:
            continue
        scores[delimiter] = (counts.count(modal) / len(counts), modal)
    if not scores:
        return ",", []
    best = max(scores, key=lambda delimiter: (scores[delimiter], -DELIMITERS.index(delimiter)))
    ties = [delimiter for delimiter in scores if delimiter != best and scores[delimiter] == scores[best]]
    return best, ties


def read_delimited(path, max_cells: int, sheet_too_large) -> list:
    """Read a delimited file into one grid, shaped exactly like a read sheet."""
    from .reader import SheetGrid

    path = Path(path)
    text, encoding = read_text(path)
    delimiter, ties = sniff_delimiter_detail(text[:SNIFF_BYTES])

    rows: list[list] = []
    error_cells: list[str] = []
    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    for row_index, raw in enumerate(reader):
        row = []
        for column_index, value in enumerate(raw):
            cleaned = value.strip()
            if cleaned in ERROR_TEXT:
                error_cells.append(f"{_reference(row_index, column_index)}={cleaned}")
                cleaned = ""
            row.append(cleaned or None)
        rows.append(row)

    rows = _trim(rows)
    width = max((len(row) for row in rows), default=0)
    if len(rows) * width > max_cells:
        raise sheet_too_large(
            f"'{path.name}' spans {len(rows)} x {width} = {len(rows) * width:,} cells, "
            f"above the {max_cells:,} ceiling"
        )

    # Ragged rows are normal in exported text -- trailing empty fields are simply not
    # written. Padding here keeps every downstream rule able to index by column.
    rows = [row + [None] * (width - len(row)) for row in rows]

    grid = SheetGrid(name=path.stem, rows=rows, error_cells=error_cells)
    grid.styles = [[None] * width for _ in rows]
    grid.source_format = f"delimited ({encoding}, {_name_of(delimiter)})"
    grid.read_flags = _read_flags(delimiter, ties, encoding)
    return [grid]


def _read_flags(delimiter, ties, encoding) -> list[str]:
    from . import flags as flag_text

    found = []
    if ties:
        others = ", ".join(_name_of(other) for other in ties)
        found.append(flag_text.check(
            f"the separator was a close call: {_name_of(delimiter)} and {others} split the "
            f"opening lines equally well; read with {_name_of(delimiter)}"
        ))
    if encoding == "cp1252":
        found.append(flag_text.info("the file is not UTF-8; read as Windows-1252"))
    elif encoding == "latin-1":
        found.append(flag_text.check(
            "the file is neither UTF-8 nor Windows-1252; read as Latin-1, so accented or "
            "special characters may be wrong"
        ))
    return found


def _trim(rows) -> list[list]:
    """Drop trailing blank lines, which most exporters leave behind."""
    while rows and all(value is None for value in rows[-1]):
        rows.pop()
    return rows


def _name_of(delimiter: str) -> str:
    return {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}.get(delimiter, delimiter)


def _reference(row_index: int, column_index: int) -> str:
    """An A1-style reference, so a CSV oddity is reported the way a sheet's would be."""
    letters = ""
    index = column_index
    while True:
        letters = chr(ord("A") + index % 26) + letters
        index = index // 26 - 1
        if index < 0:
            break
    return f"{letters}{row_index + 1}"

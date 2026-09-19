"""Turn one report-shaped sheet into a tidy DataFrame, recording every decision.

The pipeline is deliberately generic: nothing here is keyed to a particular file.
Each structural choice is made from the cell content and written into a trace so
that a reviewer can see, afterwards, why a row was dropped or a sheet transposed.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

import pandas as pd

from . import schema, typing_utils
from .typing_utils import is_blank

HEADER_SEARCH_DEPTH = 20
ORIENTATION_TIEBREAK_MARGIN = 0.2

BANNER = "BANNER"
BLANK = "BLANK"
SEPARATOR = "SEPARATOR"
SUBTOTAL = "SUBTOTAL"
FOOTER = "FOOTER"
DATA = "DATA"

_SUBTOTAL_PATTERN = re.compile(r"^\s*(sub\s*-?\s*total|grand\s+total|total)\b", re.IGNORECASE)
_FOOTER_PATTERN = re.compile(
    r"^\s*(\*+\s*end of report|end of report|for queries|confidential|report generated on|"
    r"prepared by|page \d+|source:)",
    re.IGNORECASE,
)


@dataclass
class SheetResult:
    """A cleaned sheet plus the trace explaining how it was cleaned."""

    sheet_name: str
    frame: pd.DataFrame
    trace: dict = field(default_factory=dict)


def extract_sheet(grid) -> SheetResult:
    """Run the full extraction pipeline over one SheetGrid."""
    trace: dict = {
        "sheet": grid.name,
        "raw_shape": [grid.height, grid.width],
        "error_cells_nulled": grid.error_cells,
        "merged_ranges": grid.merged_ranges,
        "dropped_rows": [],
        "notes": [],
    }

    rows = _bound_block(grid.rows, trace)
    if not rows:
        trace["notes"].append("sheet is empty after bounding; nothing extracted")
        return SheetResult(grid.name, pd.DataFrame(), trace)

    rows = _orient(rows, trace)
    header_index = _find_header_row(rows, trace)
    header = rows[header_index]
    body = rows[header_index + 1 :]

    # Banner rows sit above the header: titles, timestamps, confidentiality notices.
    for offset, banner_row in enumerate(rows[:header_index]):
        if not _is_blank_row(banner_row):
            _record_drop(trace, offset, BANNER, banner_row)

    header, body = _drop_gutter_columns(header, body, trace)
    column_names, mapping = _name_columns(header, trace)
    body = _classify_and_strip(body, column_names, header_index, trace)

    frame = pd.DataFrame(body, columns=column_names)
    frame = _coerce_dtypes(frame, trace)
    _validate(frame, trace)

    trace["clean_shape"] = list(frame.shape)
    trace["canonical_fields"] = sorted(set(mapping.values()))
    return SheetResult(grid.name, frame, trace)


# --------------------------------------------------------------------------- #
# Step 2: bound the populated block
# --------------------------------------------------------------------------- #


def _is_blank_row(row) -> bool:
    return all(is_blank(value) for value in row)


def _bound_block(rows, trace) -> list[list]:
    """Trim fully-empty rows and columns from the outside of the sheet."""
    kept = [index for index, row in enumerate(rows) if not _is_blank_row(row)]
    if not kept:
        return []
    first_row, last_row = kept[0], kept[-1]

    width = max((len(row) for row in rows), default=0)
    populated_columns = {
        index
        for row in rows[first_row : last_row + 1]
        for index in range(min(len(row), width))
        if not is_blank(row[index])
    }
    if not populated_columns:
        return []
    first_column, last_column = min(populated_columns), max(populated_columns)

    if first_row or first_column:
        trace["notes"].append(
            f"leading empty gutter trimmed: {first_row} row(s), {first_column} column(s)"
        )

    block = []
    for row in rows[first_row : last_row + 1]:
        padded = list(row) + [None] * (last_column + 1 - len(row))
        block.append(padded[first_column : last_column + 1])
    trace["block_origin"] = {"row": first_row, "column": first_column}
    return block


# --------------------------------------------------------------------------- #
# Step 3: orientation
# --------------------------------------------------------------------------- #


def _best_alias_rate(lines) -> float:
    """Highest alias match rate among the leading lines of one axis."""
    return max(
        (schema.match_aliases(line)[0] for line in lines[:HEADER_SEARCH_DEPTH]),
        default=0.0,
    )


def _orient(rows, trace) -> list[list]:
    """Transpose the block if the fields run down column 1 instead of across row 1.

    The primary vote is the alias match rate: because the target schema is known
    ahead of time, the axis whose first line reads as a list of field names is the
    field axis. Type homogeneity only breaks near-ties, where it is least likely
    to be fooled by a schema that is mostly strings.
    """
    columns = [list(column) for column in zip(*rows)]
    # Score a band of leading lines on each axis, not just line 1: a banner can sit
    # above the header (File2's title block), so the field names are not guaranteed
    # to be the first thing on their own axis.
    row_rate = _best_alias_rate(rows)
    column_rate = _best_alias_rate(columns)

    vote = {"row_alias_rate": round(row_rate, 3), "column_alias_rate": round(column_rate, 3)}
    decided_by = "alias_match"

    if abs(row_rate - column_rate) < ORIENTATION_TIEBREAK_MARGIN:
        # Ambiguous on names alone -- fall back to which axis is type-homogeneous.
        # In a normal table each column holds one type; in a transposed one each row does.
        column_homogeneity = typing_utils.mean_homogeneity(columns[1:] or columns)
        row_homogeneity = typing_utils.mean_homogeneity(rows[1:] or rows)
        vote["column_homogeneity"] = round(column_homogeneity, 3)
        vote["row_homogeneity"] = round(row_homogeneity, 3)
        decided_by = "type_homogeneity"
        transposed = row_homogeneity > column_homogeneity
    else:
        transposed = column_rate > row_rate

    vote["decided_by"] = decided_by
    vote["orientation"] = "transposed" if transposed else "normal"
    trace["orientation"] = vote

    return columns if transposed else rows


# --------------------------------------------------------------------------- #
# Steps 4-6: header band, gutter columns, column names
# --------------------------------------------------------------------------- #


def _find_header_row(rows, trace) -> int:
    """Pick the row that reads most like a list of field names.

    Not "the first populated row" -- File2's title banner would win. Not "the first
    row with no gaps" -- File1's header spans B2:J2 with an empty gutter at F2.
    """
    best_index, best_rate = 0, -1.0
    scores = []
    for index, row in enumerate(rows[:HEADER_SEARCH_DEPTH]):
        rate, mapping = schema.match_aliases(row)
        scores.append({"row": index, "alias_rate": round(rate, 3), "matched": len(mapping)})
        # Strictly greater keeps the topmost row on a tie, which is where a real
        # header sits when data rows happen to echo field names.
        if rate > best_rate:
            best_index, best_rate = index, rate

    trace["header"] = {"row_index": best_index, "alias_rate": round(best_rate, 3), "scores": scores}
    if best_rate == 0.0:
        trace["notes"].append(
            "no row matched the canonical schema; falling back to the first row as header"
        )
    return best_index


def _drop_gutter_columns(header, body, trace):
    """Drop columns that are empty in the header *and* in every data row.

    This has to run after the header is located, not before: File1's separator
    column F is empty top to bottom but sits between two real header cells.
    """
    keep = []
    for index in range(len(header)):
        column_values = [row[index] if index < len(row) else None for row in body]
        if is_blank(header[index]) and all(is_blank(value) for value in column_values):
            continue
        keep.append(index)

    dropped = len(header) - len(keep)
    if dropped:
        trace["notes"].append(f"{dropped} empty gutter column(s) dropped")

    new_header = [header[index] for index in keep]
    new_body = [[row[index] if index < len(row) else None for index in keep] for row in body]
    return new_header, new_body


def _name_columns(header, trace):
    """Map header labels to canonical names, keeping unrecognised ones as-is."""
    _, mapping = schema.match_aliases(header)
    names, used = [], set()
    for index, label in enumerate(header):
        if index in mapping:
            name = mapping[index]
        elif is_blank(label):
            name = f"unnamed_{index}"
        else:
            # Unmapped columns are kept, not dropped -- losing a column silently is
            # worse than carrying one with an ugly name.
            name = re.sub(r"[^a-z0-9]+", "_", str(label).casefold()).strip("_") or f"unnamed_{index}"
        while name in used:
            name = f"{name}_dup"
        used.add(name)
        names.append(name)

    trace["column_mapping"] = {
        str(header[index]): names[index]
        for index in range(len(header))
        if not is_blank(header[index])
    }
    trace["unmapped_columns"] = [
        names[index]
        for index in range(len(header))
        if index not in mapping and not is_blank(header[index])
    ]
    return names, mapping


# --------------------------------------------------------------------------- #
# Step 7: strip non-data rows
# --------------------------------------------------------------------------- #


def _first_populated(row):
    for value in row:
        if not is_blank(value):
            return str(value)
    return ""


def _classify_row(row, column_names) -> str:
    if _is_blank_row(row):
        return BLANK

    lead = _first_populated(row)
    identifying = [
        row[index]
        for index, name in enumerate(column_names)
        if name in schema.RECORD_IDENTIFYING_FIELDS and index < len(row)
    ]
    # A total row names itself in its first cell and leaves the record-identifying
    # columns empty. Requiring both keeps a legitimate profit centre called
    # "Total Risk PC" from being mistaken for a subtotal.
    identifiers_empty = bool(identifying) and all(is_blank(value) for value in identifying)

    if _SUBTOTAL_PATTERN.match(lead) and (identifiers_empty or not identifying):
        return SUBTOTAL
    if _FOOTER_PATTERN.match(lead):
        return FOOTER
    return DATA


def _classify_and_strip(body, column_names, header_index, trace):
    """Drop banner, blank, subtotal and footer rows; keep genuine records.

    Scans the whole body rather than trimming the tail, because File3's subtotals
    sit between the groups they summarise. A single blank row is treated as a
    cosmetic separator -- File1 row 8 splits two halves of one table -- so only a
    run of two or more blanks with nothing table-shaped after it ends the table.
    """
    classifications = [_classify_row(row, column_names) for row in body]

    end = len(body)
    index = 0
    while index < len(body):
        if classifications[index] != BLANK:
            index += 1
            continue
        run_end = index
        while run_end < len(body) and classifications[run_end] == BLANK:
            run_end += 1
        remaining_data = any(kind == DATA for kind in classifications[run_end:])
        if run_end - index >= 2 and not remaining_data:
            end = index
            break
        if remaining_data:
            for blank_index in range(index, run_end):
                classifications[blank_index] = SEPARATOR
        index = run_end

    kept = []
    for offset, row in enumerate(body):
        # +1 converts a body offset into a header-relative sheet row index.
        sheet_row = header_index + 1 + offset
        if offset >= end:
            _record_drop(trace, sheet_row, "TRAILING", row)
            continue
        kind = classifications[offset]
        if kind == DATA:
            kept.append(row)
        elif kind != BLANK:
            _record_drop(trace, sheet_row, kind, row)

    return kept


def _record_drop(trace, row_index, kind, row) -> None:
    content = " | ".join("" if is_blank(value) else str(value) for value in row).strip(" |")
    trace["dropped_rows"].append(
        {"row_index": row_index, "classification": kind, "content": content[:200]}
    )


# --------------------------------------------------------------------------- #
# Step 8: dtypes
# --------------------------------------------------------------------------- #


def _coerce_dtypes(frame: pd.DataFrame, trace) -> pd.DataFrame:
    """Apply the schema's target dtypes, reporting every value that fails."""
    failures: list[dict] = []
    for column in frame.columns:
        dtype = schema.dtype_of(column)
        if dtype == "float":
            frame[column] = _to_float(frame[column], column, failures)
        elif dtype == "date":
            frame[column] = _to_date(frame[column], column, failures)
        elif dtype == "zip":
            frame[column] = frame[column].map(_to_zip)
        else:
            frame[column] = frame[column].map(_to_string)
    trace["coercion_failures"] = failures
    return frame


def _to_string(value):
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        # 1005.0 must not become the identifier "1005.0".
        return str(int(value))
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()[:10]
    return str(value).strip()


def _to_zip(value):
    """US ZIP as a 5-wide string, restoring a leading zero Excel dropped."""
    text = _to_string(value)
    if text is None:
        return None
    return text.zfill(5) if text.isdigit() and len(text) <= 5 else text


def _to_float(series: pd.Series, column: str, failures: list[dict]) -> pd.Series:
    def convert(value):
        if is_blank(value):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip().replace(",", "").replace("%", "").replace("$", "")
        try:
            return float(text)
        except ValueError:
            # Never coerce silently: a value we cannot parse is a reviewable event.
            failures.append({"column": column, "value": str(value)[:80], "reason": "not numeric"})
            return None

    return series.map(convert).astype("float64")


def _to_date(series: pd.Series, column: str, failures: list[dict]) -> pd.Series:
    def convert(value):
        if is_blank(value):
            return None
        if isinstance(value, _dt.datetime):
            return value.date().isoformat()
        if isinstance(value, _dt.date):
            return value.isoformat()
        try:
            return pd.to_datetime(str(value).strip()).date().isoformat()
        except (ValueError, TypeError):
            failures.append(
                {"column": column, "value": str(value)[:80], "reason": "unparseable date"}
            )
            return None

    return series.map(convert)


# --------------------------------------------------------------------------- #
# Step 9: validation
# --------------------------------------------------------------------------- #


def _validate(frame: pd.DataFrame, trace) -> None:
    """Report data-quality findings. Nothing is dropped on a failure."""
    findings: list[dict] = []
    if frame.empty:
        trace["validation"] = findings
        return

    for column in frame.columns:
        null_count = int(frame[column].isna().sum())
        if null_count:
            findings.append(
                {
                    "check": "null_values",
                    "column": column,
                    "count": null_count,
                    "rate": round(null_count / len(frame), 3),
                }
            )

    # Uniqueness is scoped to this sheet on purpose. File5's Jan and Feb sheets
    # reuse the same policy numbers; checking across the stacked result would
    # flag every row as a duplicate.
    if "policy_number" in frame.columns:
        values = frame["policy_number"].dropna()
        duplicated = values[values.duplicated()].tolist()
        if duplicated:
            findings.append(
                {"check": "duplicate_policy_number_within_sheet", "values": duplicated[:20]}
            )

    if "premium" in frame.columns:
        negative = int((frame["premium"] < 0).sum())
        if negative:
            findings.append({"check": "negative_premium", "count": negative})

    trace["validation"] = findings

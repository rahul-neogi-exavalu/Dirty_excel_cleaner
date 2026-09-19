"""Turn one sheet into tidy DataFrames -- one per table region -- with a full trace.

Nothing in this module knows a field name. Structure is decided by `geometry`,
`header` and `rowclass`; this file sequences them, applies types, validates, and
records why every decision went the way it did.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

import pandas as pd

from . import geometry, header as header_mod, pivot as pivot_mod, rowclass, signals, typing_utils
from .typing_utils import is_blank

ORIENTATION_MARGIN = 0.3


@dataclass
class SheetResult:
    """One cleaned table region, plus the trace explaining how it was cleaned."""

    sheet_name: str
    frame: pd.DataFrame
    trace: dict = field(default_factory=dict)
    region_index: int = 0

    @property
    def label(self) -> str:
        return self.sheet_name if self.region_index == 0 else f"{self.sheet_name}_r{self.region_index + 1}"


def extract_sheet(grid) -> list[SheetResult]:
    """Extract every table region on a sheet. Returns one result per region."""
    rows, flipped = _orient_sheet(grid.rows)
    regions = geometry.find_regions(rows)
    if not regions:
        return [
            SheetResult(
                grid.name,
                pd.DataFrame(),
                {
                    "sheet": grid.name,
                    "notes": ["no table-shaped region found on this sheet"],
                    "dropped_rows": [],
                },
            )
        ]

    return [
        _extract_region(grid, region, index, len(regions), flipped)
        for index, region in enumerate(regions)
    ]


def _orient_sheet(rows):
    """Decide the sheet's orientation before it is cut into regions.

    Segmentation has to happen the right way up. In a transposed sheet a blank row is
    a blank *field*, not a table separator, and splitting on it cuts one table in half
    before anything has had the chance to notice the sheet is sideways.

    Only a decisive verdict flips the sheet. A marginal one is left alone and settled
    per region, where a small lookup table can still be judged on its own.
    """
    block = _bounded(rows)
    if len(block) < 2:
        return rows, False
    # Strip decoration before anything is measured. A title or banner is a one-cell
    # row while the sheet is upright; a merged one repeats a single value across every
    # column, which makes that row perfectly type-homogeneous and drags the whole
    # sheet towards looking transposed. Removed here, it can neither skew the vote nor
    # survive a flip as a phantom field.
    modal = signals.modal_fill(block)
    body = [row for row in block if signals.effective_fill(row) > 1] if modal > 1 else block
    if len(body) < 2:
        return rows, False

    columns = [list(column) for column in zip(*body)]
    if len(columns) < 2:
        return rows, False

    # A pivot matrix is symmetric -- every row homogeneous and every column too -- so
    # the vote has nothing to grip on and a tiebreak would flip a perfectly readable
    # table on its side. Recognise it before orientation gets the chance.
    if pivot_mod.looks_pivoted(body):
        return rows, False

    row_score, column_score = _orientation_scores(body, columns)
    if column_score - row_score < ORIENTATION_MARGIN:
        return rows, False
    return columns, True


def _bounded(rows):
    """The populated rectangle of a sheet, padded to a common width."""
    kept = [row for row in rows if not all(is_blank(cell) for cell in row)]
    if not kept:
        return []
    width = max(len(row) for row in kept)
    padded = [list(row) + [None] * (width - len(row)) for row in kept]
    used = [
        index
        for index in range(width)
        if any(not is_blank(row[index]) for row in padded)
    ]
    if not used:
        return []
    return [row[min(used) : max(used) + 1] for row in padded]


def _orientation_scores(rows, columns):
    """Score reading a block upright versus transposed.

    Each column of an upright table holds one type while each row is mixed, and a
    header line's labels are distinct while a line of records repeats values.
    """
    row_score = typing_utils.mean_homogeneity(columns[1:] or columns) + signals.uniqueness(rows[0])
    column_score = typing_utils.mean_homogeneity(rows[1:] or rows) + signals.uniqueness(columns[0])
    return row_score, column_score


def _extract_region(grid, region, index, total, sheet_flipped=False) -> SheetResult:
    trace: dict = {
        "sheet": grid.name,
        "region": index + 1,
        "regions_on_sheet": total,
        "region_shape": [region.height, region.width],
        "region_origin": {
            "row": region.row_offset,
            "column": region.column_offset,
            "how": region.origin,
        },
        "error_cells_nulled": grid.error_cells,
        "formula_cells": len(getattr(grid, "formula_cells", [])),
        "uncached_formula_cells": getattr(grid, "uncached_formula_cells", [])[:20],
        "merged_ranges": grid.merged_ranges,
        "dropped_rows": [],
        "notes": [],
    }

    if sheet_flipped:
        trace["notes"].append(
            "the whole sheet was transposed before segmentation; a blank row here is a "
            "blank field, not a table separator"
        )
        trace["orientation"] = {
            "orientation": "transposed",
            "confident": True,
            "decided_by": "sheet_level_content_scores",
        }
        rows, styles = region.rows, None
    else:
        rows, styles = _orient(region, grid, trace)
    width = max((len(row) for row in rows), default=0)

    found = header_mod.find_header(rows, width, styles)
    trace["header"] = {
        "row_index": found.row_index,
        "detected": found.detected,
        "score": found.score,
        "multi_row": found.multi_row,
        "scores": found.scores[:10],
    }
    trace["notes"].extend(found.notes)

    body_start = 0 if not found.detected else found.row_index + (2 if found.multi_row else 1)
    header_row = rows[found.row_index] if found.detected else None

    # Rows above the header are decoration by position, whatever they say.
    for offset in range(0, body_start - (2 if found.multi_row else 1) if found.detected else 0):
        if not all(is_blank(cell) for cell in rows[offset]):
            _record_drop(trace, region, offset, rowclass.BANNER, rows[offset], "sits above the header")

    body = rows[body_start:]
    names = _fit_names(found.names, width)
    names, body, width = _realign_gutters(names, found, rows, body, width, trace)

    verdicts = rowclass.classify_rows(body, header_names=names, header_row=header_row)
    kept = []
    for verdict in verdicts:
        if verdict.dropped:
            _record_drop(
                trace, region, body_start + verdict.index,
                verdict.classification, body[verdict.index], verdict.reason,
            )
        else:
            kept.append(body[verdict.index])
            if verdict.confidence < 1.0:
                trace["notes"].append(
                    f"row {region.row_offset + body_start + verdict.index} kept with low "
                    f"confidence: {verdict.reason}"
                )

    frame = pd.DataFrame([_fit_row(row, width) for row in kept], columns=names)
    frame = _coerce_types(frame, trace)
    _validate(frame, trace)

    frame = _unpivot(frame, rows, found, names, trace)

    _report_empty_columns(frame, grid, trace)
    trace["column_names"] = names
    trace["clean_shape"] = list(frame.shape)
    return SheetResult(grid.name, frame, trace, region_index=index)


# --------------------------------------------------------------------------- #
# Orientation
# --------------------------------------------------------------------------- #


def _orient(region, grid, trace):
    """Flip the region if its fields run down the first column.

    Two schema-free signals, summed. Each column of an upright table holds one type
    while each row is mixed, and a header line's labels are distinct while a line of
    records repeats values.
    """
    rows = region.rows
    columns = [list(column) for column in zip(*rows)] if rows else []
    if not columns:
        return rows, None

    # Same reasoning as at sheet level: a pivot is symmetric, so the vote is a coin
    # toss and the shape tiebreak would stand a readable matrix on its side.
    if pivot_mod.looks_pivoted(rows):
        trace["orientation"] = {
            "orientation": "normal",
            "confident": True,
            "decided_by": "pivot_layout",
        }
        trace["notes"].append(
            "columns head values of one variable, so the block is a matrix; orientation "
            "does not apply and it is read as written"
        )
        return rows, _region_styles(region, grid)

    row_score, column_score = _orientation_scores(rows, columns)
    margin = abs(row_score - column_score)
    transposed = column_score > row_score

    decided_by = "content_scores"
    if margin < ORIENTATION_MARGIN:
        # Too close to call on homogeneity and uniqueness alone -- a near-square table
        # looks plausible either way. Ask which reading produces a better *header*:
        # the axis that yields a row of labels sitting above unlike columns is the
        # right one, and that is a far stronger signal than the shape of the block.
        # Header quality was tried here as a stronger tiebreak and measured worse: on a
        # small lookup table the first column of entity names scores as well as the real
        # header, and the block gets flipped into a single row. Shape is the cruder
        # signal but the reliable one -- a table is far more often taller than wide.
        upright_records, upright_fields = len(rows) - 1, len(columns)
        transposed = upright_records < upright_fields
        decided_by = "shape"
        trace["notes"].append(
            f"orientation margin {margin:.2f} is narrow; decided by shape "
            f"({upright_records} records vs {upright_fields} fields)"
        )

    trace["orientation"] = {
        "row_score": round(row_score, 3),
        "column_score": round(column_score, 3),
        "margin": round(margin, 3),
        "confident": decided_by != "shape",
        "decided_by": decided_by,
        "orientation": "transposed" if transposed else "normal",
    }

    if not transposed:
        styles = _region_styles(region, grid)
        return rows, styles
    # Formatting cannot be mapped through a transpose meaningfully, so the emphasis
    # signal is simply unavailable for flipped regions.
    return columns, None


def _region_styles(region, grid):
    """Style rows aligned to the region's own coordinates."""
    if not getattr(grid, "styles", None):
        return None
    styles = []
    for source in region.source_rows:
        if source >= len(grid.styles):
            styles.append([])
            continue
        row = grid.styles[source]
        styles.append(row[region.column_offset : region.column_offset + region.width])
    return styles


# --------------------------------------------------------------------------- #
# Shaping
# --------------------------------------------------------------------------- #


def _fit_names(names, width):
    names = list(names[:width])
    while len(names) < width:
        names.append(f"column_{len(names) + 1}")
    return names


def _fit_row(row, width):
    row = list(row[:width])
    return row + [None] * (width - len(row))


def _unpivot(frame, rows, found, names, trace):
    """Melt a wide matrix into long form when the columns are values, not fields.

    Left wide, a month-per-column matrix produces a column per month, which no
    downstream table can take. Melted, it produces one row per cell.
    """
    if frame.empty or not found.detected:
        return frame
    detected = pivot_mod.detect(rows, found.row_index)
    if detected is None:
        return frame

    long, report = pivot_mod.melt(frame, detected, names, rows[found.row_index])
    trace["pivot"] = report
    if report.get("unpivoted"):
        trace["notes"].append(
            f"columns {report['value_columns'][:3]}... are values of one variable, not "
            f"fields; unpivoted into '{report['variable_column']}' and 'value' "
            f"({report['rows_before']} rows -> {report['rows_after']})"
        )
    return long


def _realign_gutters(names, found, rows, body, width, trace):
    """Re-seat the body under the header when the two disagree about gutter columns.

    Some exports put the decorative gap in the data rows but not in the header, so the
    header occupies columns 0-7 while every record occupies 0,1,2,4,5,6,8,9. Read
    positionally, every value after the first gap lands under the wrong label -- the
    carrier name arrives in the premium column and nothing announces the error.

    When both sides hold the same *number* of values, the disagreement is only about
    spacing, so each side is compacted and the two are zipped back together. When the
    counts differ the shapes genuinely differ, and nothing is moved -- guessing there
    would be worse than the gap.
    """
    if not found.detected:
        return names, body, width

    header_columns = [
        index for index in range(width) if not is_blank(rows[found.row_index][index])
    ]
    body_columns = [
        index
        for index in range(width)
        if any(index < len(row) and not is_blank(row[index]) for row in body)
    ]
    if not body or header_columns == body_columns:
        return names, body, width
    if len(header_columns) != len(body_columns):
        return names, body, width

    trace["notes"].append(
        f"header occupies columns {header_columns} but the data occupies {body_columns}; "
        "both hold the same number of values, so the body was re-seated under the header"
    )
    new_names = [names[index] for index in header_columns]
    new_body = [[row[index] if index < len(row) else None for index in body_columns] for row in body]
    return new_names, new_body, len(new_names)


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


def _coerce_types(frame: pd.DataFrame, trace) -> pd.DataFrame:
    """Infer each column's type from its own values.

    Without a target schema there is no authority to appeal to, so a column becomes
    whatever the majority of its values already are. Two safeguards stop identifiers
    being silently damaged: a numeric column whose values carry leading zeros or share
    one width is kept as text, and a column mixing identifiers with integers is kept as
    text as well.
    """
    failures: list[dict] = []
    inferred: dict[str, str] = {}

    for column in frame.columns:
        values = [value for value in frame[column] if not is_blank(value)]
        kind = signals.modal_type(values) if values else typing_utils.EMPTY
        types = {typing_utils.infer_type(value) for value in values}

        if kind in (typing_utils.INT, typing_utils.FLOAT) and _looks_like_a_code(values):
            kind = typing_utils.ID_STRING
            trace["notes"].append(f"column '{column}' kept as text: values look like codes")
        elif typing_utils.ID_STRING in types and types & {typing_utils.INT, typing_utils.FLOAT}:
            kind = typing_utils.ID_STRING
            trace["notes"].append(f"column '{column}' mixes identifiers and numbers; kept as text")

        # A column written in four different date formats has no single lexical
        # signature, so the type lattice sees only "text". Parseability is the honest
        # test: if most values resolve to a date, it is a date column and the stragglers
        # are reportable failures rather than the column's true nature.
        if kind in (typing_utils.TEXT, typing_utils.DATE_STRING) and _mostly_dates(values):
            kind = typing_utils.DATE
            trace["notes"].append(
                f"column '{column}' holds mixed date formats; parsed as dates"
            )

        inferred[column] = kind
        if kind == typing_utils.FLOAT or kind == typing_utils.INT:
            frame[column] = _to_float(frame[column], column, failures)
        elif kind in (typing_utils.DATE, typing_utils.DATETIME, typing_utils.DATE_STRING):
            frame[column] = _to_date(frame[column], column, failures)
        else:
            frame[column] = frame[column].map(_to_string)

    trace["inferred_types"] = inferred
    trace["coercion_failures"] = failures
    return frame


DATE_SHARE_THRESHOLD = 0.6


def _mostly_dates(values) -> bool:
    """Whether most of a column's values resolve to a real date."""
    if len(values) < 3:
        return False
    parsed = sum(1 for value in values if _parse_date(value) is not None)
    return parsed / len(values) >= DATE_SHARE_THRESHOLD


def _parse_date(value):
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value
    text = str(value).strip()
    # A bare number is a quantity, not a date; pandas would happily read it as one.
    if not text or text.replace(".", "").replace("-", "").isdigit() and len(text) < 8:
        return None
    try:
        return pd.to_datetime(text, errors="raise")
    except (ValueError, TypeError, OverflowError):
        return None


def _looks_like_a_code(values) -> bool:
    """Whether a numeric column is really an identifier that must stay text.

    Two structural tells, neither of which needs to know the field's name:

    * a leading zero, which is meaningless in a quantity and is only preserved at all
      when the cell was already text;
    * near-unique integers that all share one digit width -- account numbers, centre
      codes, branch ids. A genuine measure varies in magnitude; a code does not.

    This is what recovers, from the data alone, the decision a schema used to declare:
    ``1005`` is an identifier, not a quantity, and must not be arithmetic.
    """
    texts = [str(value).strip() for value in values]
    digits = [text for text in texts if text.isdigit()]
    if len(digits) < len(texts) or len(digits) < 3:
        return False
    if any(text.startswith("0") for text in digits):
        return True
    widths = {len(text) for text in digits}
    distinct_ratio = len(set(digits)) / len(digits)
    return len(widths) == 1 and next(iter(widths)) >= 3 and distinct_ratio >= 0.9


def _to_string(value):
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()[:10]
    return str(value).strip()


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
            failures.append({"column": column, "value": str(value)[:80], "reason": "not numeric"})
            return None

    converted = series.map(convert).astype("float64")
    # A column whose every value is whole is written as an integer. Without this a ZIP
    # code or a centre number that escaped the code heuristic lands in the CSV as
    # "75202.0", which is wrong for a downstream load and merely noise to a reader.
    present = converted.dropna()
    if len(present) and (present % 1 == 0).all():
        return converted.astype("Int64")
    return converted


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
            failures.append({"column": column, "value": str(value)[:80], "reason": "unparseable date"})
            return None

    return series.map(convert)


def _report_empty_columns(frame, grid, trace) -> None:
    """Say why a column came out empty, rather than shipping silent nulls.

    An all-null column is never just missing data. Either the source really is blank,
    or -- far more often in a generated export -- it is formula-driven and the
    workbook carries no cached results, in which case the values exist in Excel and
    simply were not available to read.
    """
    empty = [column for column in frame.columns if frame[column].isna().all()]
    if not empty:
        return
    uncached = getattr(grid, "uncached_formula_cells", [])
    for column in empty:
        if uncached:
            trace["notes"].append(
                f"column '{column}' is entirely empty; the sheet holds "
                f"{len(uncached)} formula cell(s) with no cached result, so these "
                "values exist in Excel but are absent from the file"
            )
        else:
            trace["notes"].append(f"column '{column}' is entirely empty in the source")
    trace["empty_columns"] = empty


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _validate(frame: pd.DataFrame, trace) -> None:
    """Report data-quality findings. Nothing is dropped on a failure."""
    findings: list[dict] = []
    if frame.empty:
        trace["validation"] = findings
        return

    for column in frame.columns:
        nulls = int(frame[column].isna().sum())
        if nulls:
            findings.append(
                {"check": "null_values", "column": column, "count": nulls,
                 "rate": round(nulls / len(frame), 3)}
            )

    # Uniqueness is scoped to this region. Stacked periods legitimately reuse keys, so
    # checking across a concatenation would flag every row as a duplicate.
    for column in _key_like_columns(frame):
        values = frame[column].dropna()
        duplicated = values[values.duplicated()].tolist()
        if duplicated:
            findings.append(
                {"check": "duplicate_key_within_region", "column": column,
                 "values": [str(value) for value in duplicated[:20]]}
            )

    trace["validation"] = findings


KEY_SHAPE_RATIO = 0.5


def _key_like_columns(frame: pd.DataFrame) -> list[str]:
    """Columns whose values are shaped like identifiers.

    Distinctness cannot be the test here, because the thing being looked for *is* a
    duplicate. A key that repeats across two sections has a low distinct ratio, and
    requiring a high one would mean the column stops counting as a key exactly when it
    has the problem worth reporting. Shape decides instead.
    """
    keys = []
    for column in frame.columns:
        values = frame[column].dropna()
        if len(values) < 3:
            continue
        if values.map(_is_identifier).mean() >= 1.0 and values.nunique() / len(values) >= KEY_SHAPE_RATIO:
            keys.append(column)
    return keys


def _is_identifier(value) -> bool:
    return typing_utils.infer_type(value) == typing_utils.ID_STRING


def _record_drop(trace, region, offset, kind, row, reason) -> None:
    content = " | ".join("" if is_blank(cell) else str(cell) for cell in row).strip(" |")
    trace["dropped_rows"].append(
        {
            "sheet_row": region.source_rows[offset] + 1 if offset < len(region.source_rows) else None,
            "region_row": offset,
            "classification": kind,
            "reason": reason,
            "content": content[:200],
        }
    )

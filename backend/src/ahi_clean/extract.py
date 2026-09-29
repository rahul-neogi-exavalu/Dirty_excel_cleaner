"""Turn one sheet into tidy DataFrames -- one per table region -- with a full trace.

Nothing in this module knows a field name. Structure is decided by `geometry`,
`header` and `rowclass`; this file sequences them, applies types, validates, and
records why every decision went the way it did.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

import polars as pl

from . import flags as flag_text
from . import (
    coerce,
    geometry,
    header as header_mod,
    pivot as pivot_mod,
    rowclass,
    signals,
    typing_utils,
)
from .typing_utils import is_blank

ORIENTATION_MARGIN = 0.3


@dataclass
class SheetResult:
    """One cleaned table region, plus the trace explaining how it was cleaned."""

    sheet_name: str
    frame: pl.DataFrame
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
        results = [
            SheetResult(
                grid.name,
                pl.DataFrame(),
                {
                    "sheet": grid.name,
                    "notes": ["no table-shaped region found on this sheet"],
                    "dropped_rows": [],
                },
            )
        ]
    else:
        results = [
            _extract_region(grid, region, index, len(regions), flipped)
            for index, region in enumerate(regions)
        ]
    _record_outside_rows(grid, rows, flipped, regions, results)
    return results


def _record_outside_rows(grid, rows, flipped, regions, results) -> None:
    """Log every non-blank line no table region took, so nothing leaves without a reason.

    Region finding keeps only blocks that can be a table. A lone title fenced off by
    blank rows -- a company name under the logo, a one-line note far below the data --
    is rejected as "not a table" and, before this, simply vanished: neither kept nor
    logged. Such a line is recorded as a banner when a table follows it and as a footer
    otherwise. A line holding nothing but Excel error values is recorded as such rather
    than passing for blank.

    These rows sit outside every region, so each is marked ``outside_region`` and the
    in-region row-conservation contract leaves them out of its own arithmetic.
    """
    covered = {index for region in regions for index in region.source_rows}
    errors = grid.error_lines(transposed=flipped) if hasattr(grid, "error_lines") else {}
    starts = sorted((region.source_rows[0], position) for position, region in enumerate(regions) if region.source_rows)
    for index, line in enumerate(rows):
        if index in covered:
            continue
        blank = all(is_blank(cell) for cell in line)
        if blank and index not in errors:
            continue
        below = next((position for start, position in starts if start > index), None)
        if blank:
            kind = rowclass.EXCEL_ERROR
            reason = f"holds only Excel error values ({', '.join(dict.fromkeys(errors[index]))}), no data"
        elif not regions:
            kind, reason = rowclass.BANNER, "no table was found on this sheet"
        elif below is not None:
            kind, reason = rowclass.BANNER, "sits above the table, apart from it"
        else:
            kind, reason = rowclass.FOOTER, "sits below the table, apart from it"
        # Kept with the table it belongs to; a sheet with no table keeps it on its one result.
        owner = results[below if below is not None else len(results) - 1]
        content = " | ".join("" if is_blank(cell) else str(cell) for cell in line).strip(" |")
        owner.trace["dropped_rows"].append(
            {
                "sheet_row": index + 1,
                "region_row": None,
                "outside_region": True,
                "classification": kind,
                "reason": reason,
                "content": (content or " | ".join(errors.get(index, [])))[:200],
            }
        )


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

    Measured on a sample. Which way up a block is, is a property of its whole shape,
    and scanning a million rows to decide it costs as much as everything else put
    together while changing no answer.
    """
    sampled_rows = signals.sampled(rows[1:] or rows)
    sampled_columns = [signals.sampled(column) for column in (columns[1:] or columns)]
    row_score = typing_utils.mean_homogeneity(sampled_columns) + signals.uniqueness(rows[0])
    column_score = typing_utils.mean_homogeneity(sampled_rows) + signals.uniqueness(
        signals.sampled(columns[0])
    )
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
    # The header as the sheet wrote it, per column name: typing reads it for one tie only.
    labels = (
        {name: label for name, label in zip(names, header_row) if not is_blank(label)}
        if header_row is not None else {}
    )
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
                trace.setdefault("low_confidence_rows", []).append(
                    region.source_rows[body_start + verdict.index] + 1
                    if body_start + verdict.index < len(region.source_rows) else None
                )
                trace["notes"].append(
                    f"row {region.row_offset + body_start + verdict.index} kept with low "
                    f"confidence: {verdict.reason}"
                )

    _flag_structure(names, found, trace, grid, region)
    frame = _typed_frame([_fit_row(row, width) for row in kept], names, trace, labels)
    _validate(frame, trace)

    frame = _unpivot(frame, rows, found, names, trace)

    _report_empty_columns(frame, grid, trace)
    trace["column_names"] = names
    trace["clean_shape"] = list(frame.shape)
    return SheetResult(grid.name, frame, trace, region_index=index)


# A header chosen with a score this close to the cut-off (0.55) was nearly refused.
HEADER_LOW_CONFIDENCE = 0.65


def _flag_structure(names, found, trace, grid=None, region=None) -> None:
    """Flag every column whose name or placement was a decision rather than a reading.

    Also carries the table-, sheet- and file-level edge cases onto every column: they
    are not properties of one column, but a reader of the metadata needs to see them.
    """
    _flag_table(names, found, trace, grid, region)
    if not found.detected:
        flag_text.add_to_all(trace, names, flag_text.check(flag_text.NO_HEADER))
    else:
        for name in names:
            if re.fullmatch(r"column_\d+", name):
                flag_text.add(trace, name, flag_text.check(flag_text.BLANK_HEADER))
            elif re.fullmatch(r"col_\d+", name):
                flag_text.add(trace, name, flag_text.info(
                    f"the header was the number {name[4:]}; named {name} so it is a valid name"
                ))
            base = re.fullmatch(r"(.+)_(\d+)", name)
            if base and base.group(1) in names:
                flag_text.add(trace, name, flag_text.check(
                    f"the header '{base.group(1)}' appears more than once; this copy was "
                    f"renamed {name}"
                ))
        if found.multi_row:
            flag_text.add_to_all(trace, names, flag_text.info(
                "the header spanned two rows; the two labels were joined"
            ))

    if trace.get("realigned"):
        flag_text.add_to_all(trace, names, flag_text.info(
            "the data sat in different columns from the header (a gap in one but not the "
            "other); values were moved back under their labels"
        ))

    orientation = trace.get("orientation", {})
    if orientation.get("orientation") == "transposed":
        flag_text.add_to_all(trace, names, flag_text.info(
            "the table was sideways (fields ran down the sheet); it was turned upright"
        ))
    if orientation and not orientation.get("confident", True):
        flag_text.add_to_all(trace, names, flag_text.check(
            "which way up this table is was a close call, decided by its shape; check "
            "it is not sideways"
        ))


def _flag_table(names, found, trace, grid, region) -> None:
    """Edge cases about the file, the sheet, the table's boundaries and its rows."""
    every = lambda flag: flag_text.add_to_all(trace, names, flag)  # noqa: E731

    for flag in getattr(grid, "read_flags", []) or []:
        every(flag)
    for note in getattr(region, "close_calls", []) or []:
        every(flag_text.check(f"table boundary was a close call: {note}"))

    if found.detected and found.score < HEADER_LOW_CONFIDENCE:
        every(flag_text.check(
            f"the header row was chosen with low confidence (score {found.score:.2f}, "
            f"cut-off {header_mod.HEADER_THRESHOLD}); check these are the real column names"
        ))
    mismatch = trace.get("alignment_mismatch")
    if mismatch:
        every(flag_text.check(
            f"the header sits in columns {mismatch['header']} but the data in "
            f"{mismatch['data']}, and they could not be lined up; check each value is "
            "under the right header"
        ))

    errors = getattr(grid, "error_cells", []) or []
    if errors:
        every(flag_text.check(
            f"{len(errors)} Excel error value(s) on this sheet (#REF!, #N/A ...) were left "
            f"empty ({flag_text.example(errors)})"
        ))
    uncached = getattr(grid, "uncached_formula_cells", []) or []
    if uncached:
        every(flag_text.check(
            f"{len(uncached)} formula cell(s) on this sheet have no saved result and are "
            f"empty in the output ({flag_text.example(uncached)}); open and re-save the "
            "workbook in Excel"
        ))
    tall_merges = [
        merged for merged in getattr(grid, "merged_ranges", []) or []
        if _merge_spans_rows(merged)
    ]
    if tall_merges:
        every(flag_text.info(
            f"{len(tall_merges)} merged cell range(s) spanning several rows were filled "
            f"with their value in every row ({flag_text.example(tall_merges)})"
        ))

    shaky = trace.get("low_confidence_rows", [])
    if shaky:
        every(flag_text.check(
            f"{len(shaky)} row(s) kept although they look incomplete (sheet rows "
            f"{flag_text.example(shaky, 5)}); check they are real records"
        ))
    unverified = [
        row["sheet_row"] for row in trace.get("dropped_rows", [])
        if row["classification"] == rowclass.UNVERIFIED_TOTAL
    ]
    if unverified:
        every(flag_text.check(
            f"{len(unverified)} row(s) labelled as totals were dropped although their "
            f"figures did not add up (sheet rows {flag_text.example(unverified, 5)}); "
            "check nothing real was removed"
        ))


def _merge_spans_rows(merged: str) -> bool:
    """Whether a merged range such as A5:A7 covers more than one row."""
    found = re.fullmatch(r"[A-Z]+(\d+):[A-Z]+(\d+)", merged)
    return bool(found) and found.group(1) != found.group(2)


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
    # Bounds hoisted out of the loop: they do not change per row, and reading them
    # inside it is what made this quadratic.
    start = region.column_offset
    stop = start + region.width

    styles = []
    for source in region.source_rows:
        if source >= len(grid.styles):
            styles.append([])
            continue
        styles.append(grid.styles[source][start:stop])
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
    if frame.is_empty() or not found.detected:
        return frame
    detected = pivot_mod.detect(rows, found.row_index)
    if detected is None:
        return frame

    long, report = pivot_mod.melt(frame, detected, names, rows[found.row_index])
    trace["pivot"] = report
    if report.get("unpivoted"):
        melted = report["value_columns"]
        # The value columns no longer exist; their flags would point at nothing.
        for column in melted:
            trace.get("flags", {}).pop(column, None)
        flag_text.add(trace, report["variable_column"], flag_text.info(
            f"created by unpivoting {len(melted)} columns that were values, not fields "
            f"({', '.join(melted[:3])}...); one row per cell"
        ))
        flag_text.add(trace, "value", flag_text.info(
            f"the cells of the {len(melted)} unpivoted columns"
        ))
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
        # Different numbers of values: nothing can be moved safely. When each side has
        # columns the other lacks, the two are offset and that deserves a flag. A blank
        # header cell over data, or a label over an empty column, is not a shift and has
        # its own flag.
        if set(header_columns) - set(body_columns) and set(body_columns) - set(header_columns):
            trace["alignment_mismatch"] = {"header": header_columns, "data": body_columns}
        return names, body, width

    trace["notes"].append(
        f"header occupies columns {header_columns} but the data occupies {body_columns}; "
        "both hold the same number of values, so the body was re-seated under the header"
    )
    trace["realigned"] = True
    new_names = [names[index] for index in header_columns]
    new_body = [[row[index] if index < len(row) else None for index in body_columns] for row in body]
    return new_names, new_body, len(new_names)


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #


def _typed_frame(rows, names, trace, labels=None) -> pl.DataFrame:
    """Build the frame column-by-column, typing each one as it is built.

    Columns rather than rows because that is the shape every type decision is made in:
    a whole column is tried against one strategy at a time, vectorised, instead of each
    cell being tried against every strategy in Python. There is deliberately no
    intermediate untyped frame -- it would cost a full copy and answer nothing.
    """
    failures: list[dict] = []
    inferred: dict[str, str] = {}
    series: list[pl.Series] = []

    for index, name in enumerate(names):
        values = [row[index] if index < len(row) else None for row in rows]
        result = coerce.coerce_column(name, values, (labels or {}).get(name))
        inferred[name] = result.kind
        for flag in result.flags:
            flag_text.add(trace, name, flag)
        failures.extend(result.failures)
        trace["notes"].extend(result.notes)
        series.append(result.series)

    trace["inferred_types"] = inferred
    trace["coercion_failures"] = failures
    return pl.DataFrame(series) if series else pl.DataFrame()


def _report_empty_columns(frame, grid, trace) -> None:
    """Say why a column came out empty, rather than shipping silent nulls.

    An all-null column is never just missing data. Either the source really is blank,
    or -- far more often in a generated export -- it is formula-driven and the
    workbook carries no cached results, in which case the values exist in Excel and
    simply were not available to read.
    """
    empty = [column for column in frame.columns if frame[column].null_count() == frame.height]
    if not empty:
        return
    uncached = getattr(grid, "uncached_formula_cells", [])
    for column in empty:
        if uncached:
            flag_text.add(trace, column, flag_text.check(
                "entirely empty: the sheet has formulas with no saved results, so the "
                "values exist in Excel but not in the file; open and re-save it in Excel"
            ))
            trace["notes"].append(
                f"column '{column}' is entirely empty; the sheet holds "
                f"{len(uncached)} formula cell(s) with no cached result, so these "
                "values exist in Excel but are absent from the file"
            )
        else:
            flag_text.add(trace, column, flag_text.info("entirely empty in the source"))
            trace["notes"].append(f"column '{column}' is entirely empty in the source")
    trace["empty_columns"] = empty


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _validate(frame: pl.DataFrame, trace) -> None:
    """Report data-quality findings. Nothing is dropped on a failure."""
    findings: list[dict] = []
    if frame.is_empty():
        trace["validation"] = findings
        return

    for column in frame.columns:
        nulls = int(frame[column].null_count())
        if nulls:
            findings.append(
                {"check": "null_values", "column": column, "count": nulls,
                 "rate": round(nulls / len(frame), 3)}
            )

    # Uniqueness is scoped to this region. Stacked periods legitimately reuse keys, so
    # checking across a concatenation would flag every row as a duplicate.
    for column in _key_like_columns(frame):
        values = frame[column].drop_nulls()
        duplicated = values.filter(values.is_duplicated()).unique().to_list()
        if duplicated:
            findings.append(
                {"check": "duplicate_key_within_region", "column": column,
                 "values": [str(value) for value in duplicated[:20]]}
            )

    trace["validation"] = findings


KEY_SHAPE_RATIO = 0.5


def _key_like_columns(frame: pl.DataFrame) -> list[str]:
    """Columns whose values are shaped like identifiers.

    Distinctness cannot be the test here, because the thing being looked for *is* a
    duplicate. A key that repeats across two sections has a low distinct ratio, and
    requiring a high one would mean the column stops counting as a key exactly when it
    has the problem worth reporting. Shape decides instead.
    """
    keys = []
    for column in frame.columns:
        values = frame[column].drop_nulls()
        if len(values) < 3:
            continue
        sampled = coerce.sample(values.to_list())
        if not all(_is_identifier(value) for value in sampled):
            continue
        if values.n_unique() / len(values) >= KEY_SHAPE_RATIO:
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

#!/usr/bin/env python
"""Score the cleaner against every scenario workbook, objectively.

The oracle is the files themselves. Every genuine record in this corpus carries a
policy number of the form ``POL-nnnnnn``; banners, totals, footers and repeated
headers do not. Counting those tokens in the raw grid therefore gives an expected
record count that owes nothing to the pipeline being tested.

    python tools/scorecard.py [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ahi_clean import orchestrate  # noqa: E402
from ahi_clean.extract import extract_sheet  # noqa: E402
from ahi_clean.reader import read_workbook  # noqa: E402

SAMPLES = ROOT / "sample_files_uncleaned"
POLICY = re.compile(r"\bPOL-\d{4,}\b")
SKIP_SHEETS = ("SCENARIO_INFO", "README")


def expected_records(grids) -> int:
    """How many policy numbers the workbook actually contains, duplicates included."""
    total = 0
    for grid in grids:
        if grid.name.upper() in SKIP_SHEETS:
            continue
        for row in grid.rows:
            for cell in row:
                if cell is not None and POLICY.fullmatch(str(cell).strip()):
                    total += 1
    return total


def scenario_of(grids) -> str:
    for grid in grids:
        if grid.name.upper() != "SCENARIO_INFO":
            continue
        for row in grid.rows:
            values = [str(cell) for cell in row if cell is not None]
            if len(values) >= 2 and values[0].strip().lower() == "scenario":
                return values[1]
    return ""


def evaluate(path: Path) -> dict:
    grids = [grid for grid in read_workbook(path) if grid.name.upper() not in SKIP_SHEETS]
    all_grids = read_workbook(path)
    expected = expected_records(all_grids)

    results = [result for grid in grids for result in extract_sheet(grid)]
    outputs, report = orchestrate.plan_workbook(results, path.stem)

    # Lookup tables carry no policy numbers, so they are excluded from the count.
    record_outputs = [
        output
        for output in outputs
        if output.kind != "dimension" and _has_policy_column(output.frame)
    ]
    got = sum(len(output.frame) for output in record_outputs)

    # A defect means the output is wrong. An advisory means the output is right but the
    # pipeline is telling a reviewer it was not certain -- those are a feature, and
    # counting them as failures would push the design towards false confidence.
    issues, advisories = [], []

    # A melted matrix carries no policy numbers, so the token oracle has nothing to
    # count. Conservation is the oracle instead: unpivoting must turn every value cell
    # into exactly one row, no more and no less.
    melted = [
        result.trace["pivot"]
        for result in results
        if result.trace.get("pivot", {}).get("unpivoted")
    ]
    if melted and not expected:
        for report, result in zip(melted, [r for r in results if r.trace.get("pivot", {}).get("unpivoted")]):
            cells = report["rows_before"] * len(report["value_columns"])
            if len(result.frame) != cells:
                issues.append(f"unpivot lost cells: {len(result.frame)} rows from {cells} values")
        expected = got = sum(
            report["rows_before"] * len(report["value_columns"]) for report in melted
        )

    if not expected:
        # No policy numbers means the oracle has nothing to compare against, and a
        # silent "0 == 0" would score an unchecked file as a pass. It did exactly that
        # for a pivot matrix the pipeline had mangled. An absent oracle is reported as
        # an absent oracle.
        issues.append("no oracle: workbook carries no policy numbers, records unverified")
    elif got != expected:
        issues.append(f"records {got} != {expected}")

    headerless = [
        result
        for result in results
        if not result.trace.get("header", {}).get("detected", True)
        and not result.trace.get("header", {}).get("names_adopted")
    ]
    if headerless:
        issues.append(f"{len(headerless)} headerless region(s)")

    positional = sum(
        1
        for result in results
        for name in result.frame.columns
        if name.startswith("column_") and result.trace.get("header", {}).get("detected")
    )
    if positional:
        advisories.append(f"{positional} column(s) named positionally (header had no label)")

    lowconf = [
        result for result in results if not result.trace.get("orientation", {}).get("confident", True)
    ]
    if lowconf:
        advisories.append(f"{len(lowconf)} orientation(s) decided by shape, flagged")

    # An all-null column is only a defect when nothing explains it. A formula-driven
    # column with no cached result, or a genuinely blank source column, is reported in
    # the trace and is the correct outcome.
    # Only a formula with no cached result actually excuses an empty column. "Empty in
    # the source" is a description, not an explanation -- and taking it as one hid a
    # column-misalignment bug that emptied two real columns.
    explained = {
        name
        for result in results
        for name in result.trace.get("empty_columns", [])
        if result.trace.get("formula_cells")
    }
    empties = [
        name
        for output in record_outputs
        for name in output.frame.columns
        if output.frame[name].null_count() == output.frame.height and name not in explained
    ]
    if empties:
        # An empty column is worth surfacing but is not by itself wrong -- a report can
        # carry a header with nothing under it. What would be wrong is a column emptied
        # by misalignment, and that is caught below by its own check.
        advisories.append(f"all-null column(s): {', '.join(sorted(set(empties))[:3])}")

    # Columns emptied because the body sat under the wrong labels. Whenever the header
    # and the data disagree about which columns they occupy, the extractor must say it
    # re-seated them; silence there means values landed under the wrong names.
    for result in results:
        header_row = result.trace.get("header", {}).get("row_index")
        if header_row is None:
            continue
        if result.trace.get("formula_cells"):
            continue  # already accounted for: the values live in Excel, not in the file
        reseated = any("re-seated" in note for note in result.trace.get("notes", []))
        blanks = [name for name in result.frame.columns if result.frame[name].null_count() == result.frame.height]
        if blanks and not reseated and len(blanks) > 1:
            issues.append(f"{len(blanks)} columns emptied with no realignment reported")

    # A one-row frame several columns wide is almost always a lookup table that was
    # wrongly transposed. Lookups carry no policy numbers, so the record count alone
    # cannot see this -- without the check a regression here scores as an improvement.
    flattened = [
        result
        for result in results
        if len(result.frame) == 1 and len(result.frame.columns) >= 4
    ]
    if flattened:
        issues.append(f"{len(flattened)} table(s) collapsed to a single row")

    # A sheet read sideways holds exactly one table -- its fields run down a column,
    # so there is no second table to find. More than one transposed region therefore
    # means one table was cut at a blank field. The record count cannot see this,
    # because only one half carries the policy numbers.
    by_sheet: dict[str, int] = {}
    for result in results:
        if result.trace.get("orientation", {}).get("orientation") == "transposed":
            by_sheet[result.sheet_name] = by_sheet.get(result.sheet_name, 0) + 1
    for sheet, count in by_sheet.items():
        if count > 1:
            issues.append(f"sheet '{sheet}' yielded {count} transposed regions -- one table split")

    return {
        "file": path.name,
        "scenario": scenario_of(all_grids),
        "expected": expected,
        "got": got,
        "regions": len(results),
        "outputs": len(outputs),
        "issues": issues,
        "advisories": advisories,
        "results": results,
        "report": report,
    }


def _has_policy_column(frame) -> bool:
    if frame.is_empty():
        return False
    for name in frame.columns:
        values = [str(value) for value in frame[name].drop_nulls().to_list()]
        if values and sum(bool(POLICY.fullmatch(value)) for value in values) / len(values) > 0.5:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="show dropped rows per file")
    parser.add_argument("--filter", default="", help="only files containing this text")
    args = parser.parse_args()

    paths = sorted(path for path in SAMPLES.glob("*.xlsx") if "master" not in path.name.lower())
    if args.filter:
        paths = [path for path in paths if args.filter in path.name]

    if not paths:
        print("no scenario workbooks matched", file=sys.stderr)
        return 1

    rows, failures = [], 0
    for path in paths:
        try:
            outcome = evaluate(path)
        except Exception as error:  # a crash is itself a result worth reporting
            rows.append((path.name, "", "-", "-", [f"CRASH: {type(error).__name__}: {error}"], []))
            failures += 1
            continue
        if outcome["issues"]:
            failures += 1
        rows.append(
            (outcome["file"], outcome["scenario"], outcome["expected"], outcome["got"],
             outcome["issues"], outcome["advisories"])
        )
        if args.verbose and outcome["issues"]:
            for result in outcome["results"]:
                for dropped in result.trace.get("dropped_rows", []):
                    print(f"      {dropped['classification']:<16} {dropped['content'][:46]}")

    width = max(len(row[0]) for row in rows)
    print(f"{'file':<{width}}  {'exp':>4} {'got':>4}  status")
    print("-" * (width + 40))
    for name, _scenario, expected, got, issues, advisories in rows:
        if issues:
            status = "FAIL  " + "; ".join(issues)
        elif advisories:
            status = "ok*   " + "; ".join(advisories)
        else:
            status = "ok"
        print(f"{name:<{width}}  {expected:>4} {got:>4}  {status}")

    advised = sum(1 for row in rows if not row[4] and row[5])
    print("-" * (width + 40))
    print(f"{len(rows) - failures}/{len(rows)} correct   ({advised} of them carry an advisory)")
    if failures:
        counts = Counter(issue.split()[0] for row in rows for issue in row[4])
        print("defects:", dict(counts))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

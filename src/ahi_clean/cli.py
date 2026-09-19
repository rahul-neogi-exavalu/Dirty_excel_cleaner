"""Command line entry point: clean a set of workbooks into CSVs plus audit reports."""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from . import audit, orchestrate
from .extract import extract_sheet
from .reader import count_embedded_images, read_workbook


def clean_workbook(source: Path, out_dir: Path, audit_dir: Path) -> dict:
    """Clean one workbook: write its CSVs and its audit report."""
    stem = source.stem
    grids = read_workbook(source)
    results = [extract_sheet(grid) for grid in grids]

    outputs, workbook_report = orchestrate.plan_workbook(results, stem)

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for output in outputs:
        path = out_dir / f"{output.name}.csv"
        output.frame.to_csv(path, index=False)
        written.append(path)

    report = audit.build_report(
        source,
        [result.trace for result in results],
        workbook_report,
        outputs,
        count_embedded_images(source),
    )
    report_path = audit.write_report(report, audit_dir, stem)

    return {"source": source, "written": written, "report": report_path, "outputs": outputs}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ahi_clean", description="Clean report-shaped Excel files into table-ready CSVs."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=["sample_files_uncleaned/*.xlsx"],
        help="Workbook paths or glob patterns (default: the bundled samples).",
    )
    parser.add_argument("--out", default="cleaned", help="Directory for cleaned CSVs.")
    parser.add_argument("--audit", default="audit", help="Directory for audit reports.")
    args = parser.parse_args(argv)

    paths: list[Path] = []
    for pattern in args.inputs:
        matches = [Path(match) for match in glob.glob(pattern)]
        paths.extend(matches or ([Path(pattern)] if Path(pattern).exists() else []))
    if not paths:
        print("no input workbooks matched", file=sys.stderr)
        return 1

    out_dir, audit_dir = Path(args.out), Path(args.audit)
    for source in sorted(paths):
        outcome = clean_workbook(source, out_dir, audit_dir)
        print(f"{source.name}")
        for output in outcome["outputs"]:
            sheets = ", ".join(output.sheets)
            print(f"  -> {output.name}.csv  [{output.kind}] {len(output.frame)} rows  (from {sheets})")
        print(f"  -> {outcome['report'].name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

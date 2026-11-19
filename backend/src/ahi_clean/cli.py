"""Command line entry point: clean a set of workbooks into CSVs plus audit reports."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import os
import sys
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import zipfile

from . import __version__, audit, contracts, failures, metadata, orchestrate
from .extract import extract_sheet
from .failures import Failure
from .reader import DEFAULT_MAX_CELLS, count_embedded_images, read_workbook

EXIT_OK = 0
EXIT_NO_INPUT = 1
EXIT_PROBLEMS = 2
EXIT_CONTRACT_VIOLATION = 3


SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".xlsb", ".csv", ".tsv", ".txt"}


def clean_workbook(
    source: Path,
    out_dir: Path,
    audit_dir: Path,
    max_cells: int = DEFAULT_MAX_CELLS,
    *,
    sheets=None,
    append: bool = True,
    write: bool = True,
) -> dict:
    """Clean one workbook: write its CSVs and its audit report.

    ``sheets`` limits a workbook to the named sheets (a delimited file has only one).
    ``append`` off ships every table on its own even when headers match exactly.
    ``write`` off is a dry run: everything is read, decided and checked, nothing written.

    Raises on an unreadable workbook. Callers processing a batch are expected to catch
    and classify -- see :func:`clean_batch`.
    """
    stem = source.stem
    grids = read_workbook(source, max_cells, only=set(sheets) if sheets else None)
    results = [result for grid in grids for result in extract_sheet(grid)]

    outputs, workbook_report = orchestrate.plan_outputs(results, stem, append)

    # Checked before anything is written, so a violation is reported alongside the file
    # rather than discovered downstream. The CSV is still produced -- withholding it
    # would hide the evidence someone needs to diagnose the violation.
    violations = contracts.check_all(results)
    workbook_report["contract_violations"] = [item.as_dict() for item in violations]

    # One job id per workbook, fixed before anything is written, so every CSV, its
    # metadata and the audit carry the final names -- nothing is renamed afterwards.
    orchestrate.name_files(outputs, str(uuid.uuid4()))
    if not write:
        return {"source": source, "written": [], "blocked": [], "report": None,
                "outputs": outputs, "violations": violations, "results": results}

    out_dir.mkdir(parents=True, exist_ok=True)
    written, blocked = [], []
    for output in outputs:
        path = out_dir / output.file
        if not _write(output.frame, path, blocked):
            # No CSV, so nothing for the metadata to describe.
            continue
        written.append(path)

        # Built from the typed frame just written, never re-read and re-guessed: the
        # frame's schema is the cleaner's own type decision.
        described = metadata.build(
            output.frame,
            source.name,
            output.sheet_names,
            metadata.inferred_schema(path),
            output.type_flags,
        )
        metadata_path = out_dir / output.metadata_file
        if _write(described, metadata_path, blocked):
            written.append(metadata_path)

    report = audit.build_report(
        source,
        [result.trace for result in results],
        workbook_report,
        outputs,
        count_embedded_images(source),
    )
    report_path = audit.write_report(report, audit_dir, stem)

    return {
        "source": source,
        "written": written,
        "blocked": blocked,
        "report": report_path,
        "outputs": outputs,
        "violations": violations,
        "results": results,
    }


def _write(frame, path: Path, blocked: list) -> bool:
    """Write a CSV, recording rather than raising when the file is locked."""
    try:
        frame.write_csv(path)
    except OSError as error:
        # Almost always the file is open in Excel, which locks it for writing.
        # Skip it, keep cleaning the rest, and say which files need closing.
        #
        # OSError rather than PermissionError: polars reports a Windows sharing
        # violation as a bare OSError with errno 32, where pandas raised
        # PermissionError. Catching only the narrower type silently stopped
        # handling locked files the moment the writer changed.
        if not _is_locked(error):
            raise
        blocked.append(path)
        return False
    return True


# Windows sharing violation. A locked file is a transient, user-fixable condition;
# anything else going wrong with a write is a real error and must not be swallowed.
_SHARING_VIOLATION = 32


def _is_locked(error: OSError) -> bool:
    if isinstance(error, PermissionError):
        return True
    if error.errno in (13, _SHARING_VIOLATION):
        return True
    return "used by another process" in str(error)


def _clean_one(args) -> tuple[Path, dict | None, tuple[str, str] | None]:
    """Clean one workbook and report the outcome without raising.

    Returns plain data rather than exceptions so the same function works unchanged
    whether it runs here, on a thread, or in another process -- an exception does not
    always survive being sent back across a process boundary, and its traceback never
    does.
    """
    source, out_dir, audit_dir, max_cells, options = args
    try:
        return source, clean_workbook(source, out_dir, audit_dir, max_cells, **options), None
    except Exception as error:  # noqa: BLE001 - a batch must survive any single file
        return source, None, (type(error).__name__, str(error))


def clean_batch(
    paths,
    out_dir: Path,
    audit_dir: Path,
    max_cells: int = DEFAULT_MAX_CELLS,
    workers: int = 1,
    executor: str = "thread",
    **options,
) -> tuple[list[dict], list[Failure]]:
    """Clean every workbook, surviving the ones that cannot be read.

    Each file gets its own error boundary. Without one, a single corrupt workbook takes
    the whole run down and every file after it is silently never processed -- which is
    the failure this is here to prevent.

    Files are independent -- separate inputs, separate outputs, separate audit reports --
    so they parallelise without any locking. Results are re-ordered to match the input
    order regardless of which finishes first, so a run stays diff-comparable.

    Threads are the default, on measurement rather than on theory. The expectation was
    that processes would win, because reading a workbook is pure-Python and holds the
    GIL. Measured over 24 workbooks, four threads ran 1.4x faster than sequential while
    four processes ran 3.6x *slower*: on Windows each worker is a fresh interpreter that
    must re-import polars before it can do anything, and for report-sized files that
    startup dwarfs the work. Processes remain available for batches of genuinely large
    files, where the startup is amortised.
    """
    # options: sheets, append, write -- passed through to clean_workbook unchanged.
    jobs = [(source, out_dir, audit_dir, max_cells, options) for source in paths]
    if workers <= 1 or len(jobs) <= 1:
        results = [_clean_one(job) for job in jobs]
    else:
        pool = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
        with pool(max_workers=workers) as running:
            results = list(running.map(_clean_one, jobs))

    order = {source: index for index, source in enumerate(paths)}
    results.sort(key=lambda item: order[item[0]])

    outcomes: list[dict] = []
    failed: list[Failure] = []
    for source, outcome, error in results:
        if outcome is not None:
            outcomes.append(outcome)
            continue
        failure = failures.classify(source, _rebuild(error))
        if failure.kind == failures.UNEXPECTED:
            _write_note(audit_dir, source, error)
        failed.append(failure)

    return outcomes, failed


def _rebuild(error: tuple[str, str]) -> BaseException:
    """Recreate enough of a worker's exception for classification.

    Only the type name and message cross a process boundary reliably, and those are
    exactly what classification needs. `zipfile.BadZipFile` is reconstructed by name so
    a corrupt file is still told apart from an encrypted one when running in parallel.
    """
    name, message = error
    known = {"BadZipFile": zipfile.BadZipFile, "FileNotFoundError": FileNotFoundError}
    if name in known:
        return known[name](message)
    # Classification keys off the exception's type *name*, so a stand-in class carrying
    # the original name preserves every distinction the classifier makes.
    return type(name, (RuntimeError,), {})(message)


def _write_note(audit_dir: Path, source: Path, error) -> None:
    """Keep the detail of an unclassified failure, out of the console.

    A stack trace is the wrong thing to print in the middle of a five-hundred file run,
    and the wrong thing to lose entirely.
    """
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = audit_dir / f"{source.stem}.error.txt"
        if isinstance(error, BaseException):
            detail = "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            )
        else:
            detail = f"{error[0]}: {error[1]}\n"
        path.write_text(detail, encoding="utf-8")
    except OSError:
        pass  # reporting the failure matters more than recording it


def resolve_inputs(patterns, recursive: bool = False, exclude=()) -> list[Path]:
    """Expand globs, folders and literal paths into a sorted, de-duplicated list of files.

    A folder contributes every supported file in it (and below, with ``recursive``).
    A file named explicitly is kept whatever its type, so an unsupported one is reported
    rather than silently skipped. Excel's own lock files (``~$Book.xlsx``) never count,
    and names matching an ``exclude`` pattern (``*donotingest*``) are left out.
    """
    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(match) for match in glob.glob(pattern, recursive=recursive)]
        if not matches and Path(pattern).exists():
            matches = [Path(pattern)]
        for match in matches:
            if match.is_dir():
                found = match.rglob("*") if recursive else match.iterdir()
                paths.extend(p for p in found if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
            elif match.is_file():
                paths.append(match)
    return sorted({
        path for path in paths
        if not path.name.startswith("~$")
        and not any(fnmatch.fnmatch(path.name.lower(), rule.lower()) for rule in exclude)
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ahi_clean",
        description=(
            "Clean report-shaped Excel and text exports (.xlsx .xlsm .xls .xlsb .csv .tsv "
            ".txt) into table-ready CSVs, with a metadata file per CSV and an audit report "
            "per input explaining every decision."
        ),
        epilog=(
            "exit codes: 0 all clean; 1 no input matched; 2 a file could not be read or "
            "written; 3 an output contract was violated (the CSV is still written)"
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=["sample_files_uncleaned"],
        help="Files, folders or glob patterns (default: the sample folder).",
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="Also look in sub-folders of a folder input; '**' in a pattern matches folders.",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="Skip files whose name matches this pattern, e.g. '*donotingest*' (repeatable).",
    )
    parser.add_argument(
        "--sheet", dest="sheets", action="append", default=[], metavar="NAME",
        help="Clean only this sheet of each workbook (repeatable; default: every sheet).",
    )
    parser.add_argument(
        "--no-append", dest="append", action="store_false",
        help="Ship every table as its own CSV, even when two tables' headers match exactly.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read, decide and check everything, print the outcome, write nothing.",
    )
    parser.add_argument("--out", default="cleaned", help="Directory for cleaned CSVs.")
    parser.add_argument("--audit", default="audit", help="Directory for audit reports.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Process this many workbooks at once (default 1, sequential).",
    )
    parser.add_argument(
        "--executor",
        choices=("thread", "process"),
        default="thread",
        help=(
            "How to parallelise. 'thread' is the default and is measurably faster for "
            "report-sized files; 'process' pays a per-worker interpreter startup that "
            "only pays off on batches of genuinely large workbooks."
        ),
    )
    parser.add_argument(
        "--max-cells",
        type=int,
        default=DEFAULT_MAX_CELLS,
        help="Refuse a sheet larger than this many cells rather than attempting it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    paths = resolve_inputs(args.inputs, args.recursive, args.exclude)
    if not paths:
        print("no input workbooks matched", file=sys.stderr)
        return EXIT_NO_INPUT

    out_dir, audit_dir = Path(args.out), Path(args.audit)
    workers = args.workers if args.workers > 0 else (os.cpu_count() or 1)
    outcomes, failed = clean_batch(
        paths, out_dir, audit_dir, args.max_cells, workers, args.executor,
        sheets=args.sheets or None, append=args.append, write=not args.dry_run,
    )

    blocked: list[Path] = []
    violations = []
    for outcome in outcomes:
        blocked.extend(outcome["blocked"])
        violations.extend(outcome["violations"])
        print(f"{outcome['source'].name}")
        if args.dry_run:
            _print_tables(outcome["results"])
        for output in outcome["outputs"]:
            sheets = ", ".join(output.sheets)
            print(f"  -> {output.file}  [{output.kind}] {len(output.frame)} rows x "
                  f"{output.frame.width} cols  (from {sheets})")
            if not args.dry_run:
                print(f"     {output.metadata_file}")
        if outcome["report"] is not None:
            print(f"  -> {outcome['report'].name}")
    if args.dry_run:
        print("(dry run: nothing was written)")

    return _report_problems(len(paths), len(outcomes), blocked, failed, violations)


def _print_tables(results) -> None:
    """One line per table found: shape, orientation, header and what was dropped."""
    for result in results:
        trace = result.trace
        dropped: dict[str, int] = {}
        for row in trace.get("dropped_rows", []):
            dropped[row["classification"]] = dropped.get(row["classification"], 0) + 1
        orientation = (trace.get("orientation") or {}).get("orientation") or "-"
        header = trace.get("header") or {}
        found = (f"header on table row {header['row_index'] + 1}" if header.get("detected")
                 else "no header (positional names)")
        removed = ", ".join(f"{count} {kind.lower()}" for kind, count in sorted(dropped.items()))
        print(f"   table {result.label}: {len(result.frame)} x {result.frame.width}, "
              f"{orientation}, {found}" + (f"; dropped {removed}" if removed else ""))


def _report_problems(total, cleaned, blocked, failed, violations) -> int:
    if not blocked and not failed and not violations:
        return EXIT_OK

    print(file=sys.stderr)
    if failed:
        print(f"{len(failed)} of {total} workbook(s) could not be read:", file=sys.stderr)
        for failure in failed:
            print(f"  {failure.source.name}  [{failure.kind}] {failure.advice}", file=sys.stderr)
    if blocked:
        print("could not write these (open in another program? close and re-run):", file=sys.stderr)
        for path in blocked:
            print(f"  {path}", file=sys.stderr)

    if violations:
        print(f"{len(violations)} output contract violation(s):", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.table}  [{violation.check}] {violation.detail}", file=sys.stderr)

    print(f"\n{cleaned} of {total} workbook(s) cleaned.", file=sys.stderr)
    return EXIT_CONTRACT_VIOLATION if violations else EXIT_PROBLEMS


if __name__ == "__main__":
    raise SystemExit(main())

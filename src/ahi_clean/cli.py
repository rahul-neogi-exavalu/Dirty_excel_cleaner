"""Command line entry point: clean a set of workbooks into CSVs plus audit reports."""

from __future__ import annotations

import argparse
import glob
import os
import sys
import traceback
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import zipfile

from . import audit, contracts, failures, metadata, orchestrate
from .extract import extract_sheet
from .failures import Failure
from .reader import DEFAULT_MAX_CELLS, count_embedded_images, read_workbook

EXIT_OK = 0
EXIT_NO_INPUT = 1
EXIT_PROBLEMS = 2
EXIT_CONTRACT_VIOLATION = 3


def clean_workbook(source: Path, out_dir: Path, audit_dir: Path, max_cells: int = DEFAULT_MAX_CELLS) -> dict:
    """Clean one workbook: write its CSVs and its audit report.

    Raises on an unreadable workbook. Callers processing a batch are expected to catch
    and classify -- see :func:`clean_batch`.
    """
    stem = source.stem
    grids = read_workbook(source, max_cells)
    results = [result for grid in grids for result in extract_sheet(grid)]

    outputs, workbook_report = orchestrate.plan_workbook(results, stem)

    # Checked before anything is written, so a violation is reported alongside the file
    # rather than discovered downstream. The CSV is still produced -- withholding it
    # would hide the evidence someone needs to diagnose the violation.
    violations = contracts.check_all(results)
    workbook_report["contract_violations"] = [item.as_dict() for item in violations]

    out_dir.mkdir(parents=True, exist_ok=True)
    written, blocked = [], []
    for output in outputs:
        # The job id is fixed at write time, so the CSV, its metadata and the audit all
        # carry the final names -- nothing is renamed afterwards.
        output.job_id = str(uuid.uuid4())
        output.file = f"{stem}_{output.job_id}.csv"
        output.metadata_file = f"{stem}_metadata_{output.job_id}.csv"

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
    source, out_dir, audit_dir, max_cells = args
    try:
        return source, clean_workbook(source, out_dir, audit_dir, max_cells), None
    except Exception as error:  # noqa: BLE001 - a batch must survive any single file
        return source, None, (type(error).__name__, str(error))


def clean_batch(
    paths,
    out_dir: Path,
    audit_dir: Path,
    max_cells: int = DEFAULT_MAX_CELLS,
    workers: int = 1,
    executor: str = "thread",
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
    jobs = [(source, out_dir, audit_dir, max_cells) for source in paths]
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


def resolve_inputs(patterns) -> list[Path]:
    """Expand globs and literal paths into a sorted, de-duplicated list of workbooks."""
    paths: list[Path] = []
    for pattern in patterns:
        matches = [Path(match) for match in glob.glob(pattern)]
        paths.extend(matches or ([Path(pattern)] if Path(pattern).exists() else []))
    return sorted(set(paths))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ahi_clean", description="Clean report-shaped Excel files into table-ready CSVs."
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        default=[
            "sample_files_uncleaned/*.xlsx",
            "sample_files_uncleaned/*.csv",
            "sample_files_uncleaned/*.tsv",
        ],
        help="Workbook or delimited-file paths, or glob patterns (default: the samples).",
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
    args = parser.parse_args(argv)

    paths = resolve_inputs(args.inputs)
    if not paths:
        print("no input workbooks matched", file=sys.stderr)
        return EXIT_NO_INPUT

    out_dir, audit_dir = Path(args.out), Path(args.audit)
    workers = args.workers if args.workers > 0 else (os.cpu_count() or 1)
    outcomes, failed = clean_batch(
        paths, out_dir, audit_dir, args.max_cells, workers, args.executor
    )

    blocked: list[Path] = []
    violations = []
    for outcome in outcomes:
        blocked.extend(outcome["blocked"])
        violations.extend(outcome["violations"])
        print(f"{outcome['source'].name}")
        for output in outcome["outputs"]:
            sheets = ", ".join(output.sheets)
            print(f"  -> {output.file}  [{output.kind}] {len(output.frame)} rows  (from {sheets})")
            print(f"     {output.metadata_file}")
        print(f"  -> {outcome['report'].name}")

    return _report_problems(len(paths), len(outcomes), blocked, failed, violations)


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

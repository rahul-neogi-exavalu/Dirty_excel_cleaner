#!/usr/bin/env python
"""Measure the pipeline: single-sheet throughput, and batch parallelism.

Run from the project root:

    python tools/benchmark.py            # both
    python tools/benchmark.py --rows     # single-sheet scaling only
    python tools/benchmark.py --batch    # executor comparison only

Kept in the repo because the defaults it informs -- which executor, what the cell
ceiling should be -- are only defensible while the numbers behind them can be
reproduced.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ahi_clean import cli  # noqa: E402
from ahi_clean.extract import extract_sheet  # noqa: E402
from ahi_clean.reader import SheetGrid  # noqa: E402

HEADER = [
    "profitCenterName", "ProfitCenterNumber", "Producer/AgencyName",
    "InsuranceCompanyName", "Premium", "PolicyNumber",
    "AccountingEffectiveDate", "Commission%",
]


def record(index):
    return [
        f"Zone {index % 6} PC", 1005 + index, "Metro Agency Group", "Liberty",
        10000.55 + index * 1.1, f"POL-{200001 + index}",
        f"2026-01-{(index % 28) + 1:02d}", 5.5 + index % 9,
    ]


def bench_rows(sizes):
    print("single sheet, extraction only\n")
    print(f"{'rows':>10} {'seconds':>9} {'us/row':>8} {'projected 1M':>14}")
    for count in sizes:
        rows = [HEADER] + [record(index) for index in range(count)]
        start = time.perf_counter()
        results = extract_sheet(SheetGrid(name="S", rows=rows))
        elapsed = time.perf_counter() - start
        assert len(results[0].frame) == count, (len(results[0].frame), count)
        print(
            f"{count:>10,} {elapsed:>9.2f} {elapsed / count * 1e6:>8.1f} "
            f"{elapsed * (1_000_000 / count):>13.0f}s"
        )


def bench_batch(copies):
    sources = cli.resolve_inputs([str(ROOT / "sample_files_uncleaned" / "*.xlsx")])
    if not sources:
        print("no sample workbooks to benchmark")
        return

    tmp = Path(tempfile.mkdtemp())
    inputs = tmp / "in"
    inputs.mkdir()
    for index in range(copies):
        shutil.copy(sources[index % len(sources)], inputs / f"book_{index:03d}.xlsx")
    paths = cli.resolve_inputs([str(inputs / "*.xlsx")])

    print(f"\nbatch of {copies} workbooks\n")
    print(f"{'mode':<18}{'seconds':>9}{'speedup':>9}")
    baseline = None
    modes = [
        ("sequential", 1, "thread"),
        ("2 threads", 2, "thread"),
        ("4 threads", 4, "thread"),
        ("8 threads", 8, "thread"),
        ("4 processes", 4, "process"),
        ("8 processes", 8, "process"),
    ]
    for label, workers, executor in modes:
        start = time.perf_counter()
        outcomes, failed = cli.clean_batch(
            paths, tmp / f"out_{workers}_{executor}", tmp / "aud",
            workers=workers, executor=executor,
        )
        elapsed = time.perf_counter() - start
        assert len(outcomes) == copies and not failed, (len(outcomes), failed)
        baseline = baseline or elapsed
        print(f"{label:<18}{elapsed:>9.2f}{baseline / elapsed:>8.2f}x")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", action="store_true")
    parser.add_argument("--batch", action="store_true")
    parser.add_argument("--sizes", type=int, nargs="*", default=[10_000, 100_000, 1_000_000])
    parser.add_argument("--copies", type=int, default=24)
    args = parser.parse_args()

    both = not (args.rows or args.batch)
    if args.rows or both:
        bench_rows(args.sizes)
    if args.batch or both:
        bench_batch(args.copies)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

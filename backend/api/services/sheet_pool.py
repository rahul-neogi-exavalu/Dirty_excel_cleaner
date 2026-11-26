"""Read and clean a file's sheets in parallel, in a shared pool of worker processes.

Why processes: cleaning is CPU-bound Python, and under the GIL threads would run one at
a time. Each sheet is independent until the append step, so each is one task --
``sheet_worker.process_sheet`` -- and the results are put back in workbook order.

Why one shared pool: starting a Python process that imports polars and openpyxl costs
a second or two, so workers are started once and reused by every job. Concurrent jobs
(two files of a batch, or two users) queue their sheets in the same pool, which keeps
total CPU and memory bounded by ``config.CLEAN_WORKERS`` however many jobs are running.

How many sheets at once: at most two per worker are handed over -- one running, one
queued so a worker never waits. The rest wait here, not in the pool's queue, so a
workbook with hundreds of sheets is streamed through rather than all submitted at once,
and cancelling never has a long queue to drain.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Callable

from .. import config
from . import sheet_worker

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_POLL_SECONDS = 0.2


class Cancelled(Exception):
    pass


class SheetFailed(Exception):
    """A sheet could not be read or cleaned; ``__cause__`` is the original error."""

    def __init__(self, sheet: str):
        super().__init__(sheet)
        self.sheet = sheet


class WorkerCrashed(Exception):
    """A worker process died mid-sheet -- most often the machine ran out of memory."""


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            # spawn, not fork: the API process runs threads, and forking a threaded
            # process can hand the child a lock that is held forever. spawn is also what
            # Windows does anyway, so every platform behaves the same.
            _pool = ProcessPoolExecutor(
                max_workers=config.CLEAN_WORKERS,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _pool


def _discard_pool(broken: ProcessPoolExecutor) -> None:
    """Drop a pool whose worker died, so the next job gets a healthy one."""
    global _pool
    with _lock:
        if _pool is broken:
            _pool = None
    broken.shutdown(wait=False, cancel_futures=True)


def warm() -> None:
    """Start the workers in the background, e.g. when a file is uploaded, so the first
    run does not wait for them."""
    if config.CLEAN_WORKERS < 2:
        return
    try:
        pool = _get_pool()
        for _ in range(config.CLEAN_WORKERS):
            pool.submit(sheet_worker.warm)
    except Exception:  # noqa: BLE001 - warming is an optimisation, never a failure
        pass


def shutdown() -> None:
    global _pool
    with _lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def use_parallel(path: Path) -> bool:
    """Worth handing to the workers: a file big enough that the hand-off is small next to
    the work. Several sheets then run side by side; even a single sheet gains, because it
    runs outside the API process -- the service stays responsive, and two files of a batch
    really do run at once instead of taking turns on the GIL."""
    if config.CLEAN_WORKERS < 2:
        return False
    try:
        return os.path.getsize(path) >= config.PARALLEL_MIN_BYTES
    except OSError:
        return False


def run_sheets(
    path: Path,
    names: list[str],
    *,
    parallel: bool,
    cancelled: Callable[[], bool],
    on_start: Callable[[list[str]], None],
    on_done: Callable[[str, object, list], None],
) -> list[tuple[object, list]]:
    """(grid, results) for every sheet in ``names``, in that order.

    ``on_start`` gets the sheets now in progress whenever that changes; ``on_done`` is
    called once per finished sheet, in completion order. Raises ``Cancelled`` when
    ``cancelled()`` turns true, and ``SheetFailed`` (chained to the real error) for the
    first sheet that fails -- the others still running are abandoned.
    """
    if not parallel:
        return _run_inline(path, names, cancelled, on_start, on_done)
    return _run_parallel(path, names, cancelled, on_start, on_done)


def _run_inline(path, names, cancelled, on_start, on_done):
    out = []
    for name in names:
        if cancelled():
            raise Cancelled()
        on_start([name])
        try:
            grid, results = sheet_worker.process_sheet(str(path), name)
        except Exception as error:
            raise SheetFailed(name) from error
        on_done(name, grid, results)
        out.append((grid, results))
    return out


def _run_parallel(path, names, cancelled, on_start, on_done):
    pool = _get_pool()
    window = config.CLEAN_WORKERS * 2
    done: dict[int, tuple[object, list]] = {}
    running: dict[Future, int] = {}
    waiting = list(range(len(names)))

    def in_progress() -> list[str]:
        # Only sheets a worker has picked up. The pool marks a task running once it enters
        # the workers' call queue, which holds one more than there are workers; tasks are
        # taken in submission order, so the first ``CLEAN_WORKERS`` are the ones executing.
        picked = [names[index] for future, index in sorted(running.items(), key=lambda item: item[1]) if future.running()]
        return picked[: config.CLEAN_WORKERS]

    try:
        while waiting or running:
            if cancelled():
                raise Cancelled()
            while waiting and len(running) < window:
                index = waiting.pop(0)
                running[pool.submit(sheet_worker.process_sheet, str(path), names[index], True)] = index
            on_start(in_progress())

            finished, _ = wait(running, timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
            for future in finished:
                index = running.pop(future)
                try:
                    grid, results = future.result()
                except BrokenProcessPool as error:
                    _discard_pool(pool)
                    raise SheetFailed(names[index]) from WorkerCrashed(
                        "a worker process stopped unexpectedly while cleaning this sheet; "
                        "the machine may have run out of memory"
                    ).with_traceback(error.__traceback__)
                except Exception as error:
                    raise SheetFailed(names[index]) from error
                done[index] = (grid, results)
                on_done(names[index], grid, results)
    finally:
        # On success nothing is left; on failure or cancel, sheets not yet started are
        # withdrawn. A sheet already running finishes in its worker and is discarded.
        for future in running:
            future.cancel()

    return [done[index] for index in range(len(names))]

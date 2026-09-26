"""Sheets read and cleaned in worker processes give exactly what one process gives.

These start real worker processes (spawned, as on Windows), so they take a few seconds.
"""

import sys
import time
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")

from api import config  # noqa: E402
from api.services import sheet_pool  # noqa: E402
from ahi_clean.reader import read_sheet, read_workbook  # noqa: E402

LEGACY = ROOT / "legacy"
MULTI_SHEET = LEGACY / "File5_Scenario_MultiSheet.xlsx"
TWO_TABLES = LEGACY / "File6_Scenarios_MultipleSheetJoin.xlsx"


@pytest.fixture(scope="module", autouse=True)
def small_pool():
    saved = config.CLEAN_WORKERS
    config.CLEAN_WORKERS = 2
    sheet_pool.shutdown()
    yield
    sheet_pool.shutdown()
    config.CLEAN_WORKERS = saved


def _run(path, names, parallel, cancelled=lambda: False):
    started, finished = [], []
    pairs = sheet_pool.run_sheets(
        path, names, parallel=parallel, cancelled=cancelled,
        on_start=started.append, on_done=lambda name, grid, results: finished.append(name),
    )
    return pairs, started, finished


def _snapshot(pairs):
    return [
        (grid.name, grid.rows, [(r.label, r.frame.to_dicts(), r.trace.get("dropped_rows")) for r in results])
        for grid, results in pairs
    ]


@pytest.mark.parametrize("path", [MULTI_SHEET, TWO_TABLES], ids=lambda p: p.stem)
def test_parallel_matches_in_process(path):
    names = [grid.name for grid in read_workbook(path)]
    inline, _, _ = _run(path, names, parallel=False)
    parallel, started, finished = _run(path, names, parallel=True)
    assert _snapshot(parallel) == _snapshot(inline)
    assert sorted(finished) == sorted(names)
    # Results come back in workbook order whatever order the workers finished in.
    assert [grid.name for grid, _ in parallel] == names
    # The work went to worker processes, and the progress callback only ever names as
    # many sheets as there are workers. (How many overlap depends on timing, so tiny test
    # sheets are not asked to prove it.)
    assert sheet_pool._pool is not None and len(sheet_pool._pool._processes) >= 2
    assert all(len(active) <= config.CLEAN_WORKERS for active in started)


def test_a_failing_sheet_is_named(tmp_path):
    with pytest.raises(sheet_pool.SheetFailed) as caught:
        _run(MULTI_SHEET, ["Jan", "Missing"], parallel=True)
    assert caught.value.sheet == "Missing"
    assert isinstance(caught.value.__cause__, KeyError)


def test_cancel_stops_before_all_sheets_run():
    polls = {"count": 0}

    def cancelled():
        polls["count"] += 1
        return polls["count"] > 1

    with pytest.raises(sheet_pool.Cancelled):
        _run(MULTI_SHEET, ["Jan", "Feb"] * 10, parallel=True, cancelled=cancelled)


def test_the_decision_follows_file_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PARALLEL_MIN_BYTES", 10**9)
    assert not sheet_pool.use_parallel(MULTI_SHEET)
    monkeypatch.setattr(config, "PARALLEL_MIN_BYTES", 0)
    assert sheet_pool.use_parallel(MULTI_SHEET)
    monkeypatch.setattr(config, "CLEAN_WORKERS", 1)
    assert not sheet_pool.use_parallel(MULTI_SHEET)


def test_job_runs_its_sheets_in_parallel(monkeypatch, tmp_path_factory):
    from fastapi.testclient import TestClient

    work = tmp_path_factory.mktemp("workspace")
    monkeypatch.setattr(config, "UPLOAD_DIR", work / "uploads")
    monkeypatch.setattr(config, "JOB_DIR", work / "jobs")
    monkeypatch.setattr(config, "PARALLEL_MIN_BYTES", 0)
    from api.main import app

    client = TestClient(app)
    with open(TWO_TABLES, "rb") as handle:
        workbook_id = client.post("/api/workbooks", files={"file": (TWO_TABLES.name, handle)}).json()["id"]
    job_id = client.post("/api/jobs", json={"workbook_id": workbook_id, "sheets": ["Report", "Producer"], "append": False}).json()["id"]
    for _ in range(600):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert status["status"] == "succeeded", status
    assert status["parallel_workers"] == 2 and status["sheets_done"] == 2
    results = client.get(f"/api/jobs/{job_id}/results").json()
    assert [sheet["sheet"] for sheet in results["sheets"]] == ["Report", "Producer"]
    assert len(results["outputs"]) == 2


def test_merged_range_below_the_data_does_not_crash(tmp_path):
    """A merge reaching past the last populated row used to raise IndexError on read."""
    path = tmp_path / "merge.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Name", "Amount"])
    sheet.append(["a", 1])
    sheet["A3"] = "note"
    sheet.merge_cells("A3:B5")
    workbook.save(path)
    grid = read_sheet(path, sheet.title)
    assert grid.height == 3 and grid.rows[2] == ["note", "note"]

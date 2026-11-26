"""One job id per run: every CSV and metadata file a run writes carries the same id."""

import re
import sys
import time
from pathlib import Path

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ahi_clean import orchestrate  # noqa: E402

UUID = r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"
MULTI_SHEET = ROOT / "legacy" / "File5_Scenario_MultiSheet.xlsx"


def _output(name: str) -> orchestrate.Output:
    return orchestrate.Output(name, pl.DataFrame({"a": [1]}), "standalone")


def test_every_file_of_a_run_shares_the_job_id():
    outputs = [_output("book_Jan"), _output("book_Feb"), _output("book_Mar")]
    orchestrate.name_files(outputs, "job-1")
    assert [output.file for output in outputs] == ["book_Jan_job-1.csv", "book_Feb_job-1.csv", "book_Mar_job-1.csv"]
    assert [output.metadata_file for output in outputs] == [
        "book_Jan_metadata_job-1.csv", "book_Feb_metadata_job-1.csv", "book_Mar_metadata_job-1.csv"]
    assert {output.job_id for output in outputs} == {"job-1"}


def test_names_that_clash_get_a_suffix_instead_of_overwriting():
    # Labels "Q1 Sales" and "Q1-Sales" slug alike; Windows also ignores case.
    outputs = [_output("book_Q1_Sales"), _output("book_Q1_Sales"), _output("BOOK_q1_sales")]
    orchestrate.name_files(outputs, "j")
    files = [output.file.lower() for output in outputs]
    assert len(set(files)) == 3


def test_a_single_output_keeps_the_stem_and_job_id_shape():
    [output] = [_output("book")]
    orchestrate.name_files([output], "0f0e0d0c-0b0a-4908-8706-050403020100")
    assert re.fullmatch(rf"book_{UUID}\.csv", output.file)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from api import config

    work = tmp_path_factory.mktemp("workspace")
    config.WORK_DIR, config.UPLOAD_DIR, config.JOB_DIR = work, work / "uploads", work / "jobs"
    from api.main import app

    return TestClient(app)


def test_a_multi_sheet_job_names_all_its_files_with_its_own_id(client):
    with open(MULTI_SHEET, "rb") as handle:
        upload = client.post("/api/workbooks", files={"file": (MULTI_SHEET.name, handle)}).json()
    sheets = [sheet["name"] for sheet in upload["sheets"]]
    job = client.post("/api/jobs", json={"workbook_id": upload["id"], "sheets": sheets, "append": False}).json()
    assert re.fullmatch(UUID, job["id"])
    for _ in range(300):
        status = client.get(f"/api/jobs/{job['id']}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert status["status"] == "succeeded", status
    outputs = client.get(f"/api/jobs/{job['id']}/results").json()["outputs"]
    assert len(outputs) == len(sheets) > 1
    files = [output["file"] for output in outputs]
    assert all(name.endswith(f"_{job['id']}.csv") for name in files)
    assert len(set(files)) == len(files)
    # Each table keeps its own id for the API, distinct from the shared job id.
    assert len({output["id"] for output in outputs}) == len(outputs)
    on_disk = {path.name for path in _job_dir(job["id"]).iterdir()}
    assert set(files) <= on_disk


def _job_dir(job_id: str) -> Path:
    from api import config

    return config.JOB_DIR / job_id

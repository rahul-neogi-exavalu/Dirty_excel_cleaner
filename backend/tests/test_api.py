"""The HTTP layer: validation in front of the cleaner, and exports that honour renames.

Runs against the fixed workbooks in ``legacy/`` so it does not depend on whatever the
sample corpus holds.
"""

import io
import sys
import time
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

LEGACY = ROOT / "legacy"
MULTI_SHEET = LEGACY / "File5_Scenario_MultiSheet.xlsx"
TWO_TABLES = LEGACY / "File6_Scenarios_MultipleSheetJoin.xlsx"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from api import config

    work = tmp_path_factory.mktemp("workspace")
    config.WORK_DIR = work
    config.UPLOAD_DIR = work / "uploads"
    config.JOB_DIR = work / "jobs"
    from api.main import app

    return TestClient(app)


def _upload(client, path: Path):
    with open(path, "rb") as handle:
        return client.post("/api/workbooks", files={"file": (path.name, handle)})


def _run(client, workbook_id, sheets, append=True):
    response = client.post("/api/jobs", json={"workbook_id": workbook_id, "sheets": sheets, "append": append})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    for _ in range(300):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] not in ("queued", "running"):
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_lists_sheet_names_only(client):
    response = _upload(client, MULTI_SHEET)
    assert response.status_code == 201
    body = response.json()
    assert [sheet["name"] for sheet in body["sheets"]] == ["Jan", "Feb"]
    # No raw row/column counts: they are not meaningful before cleaning.
    assert set(body["sheets"][0]) == {"name", "hidden", "has_content"}


def test_rejects_legacy_xls_with_advice(client):
    response = client.post("/api/workbooks", files={"file": ("old.xls", io.BytesIO(b"abc"))})
    assert response.status_code == 415
    assert "re-save" in response.json()["error"]["advice"]


def test_rejects_corrupt_xlsx(client):
    response = client.post("/api/workbooks", files={"file": ("bad.xlsx", io.BytesIO(b"not a zip"))})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "corrupt"


def test_rejects_empty_or_unknown_sheet_selection(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    empty = client.post("/api/jobs", json={"workbook_id": workbook_id, "sheets": []})
    assert empty.status_code == 422
    unknown = client.post("/api/jobs", json={"workbook_id": workbook_id, "sheets": ["Nope"]})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_sheet"


def test_results_not_available_before_success(client):
    assert client.get("/api/jobs/missing/results").status_code == 404


def test_append_on_merges_identical_headers(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    status = _run(client, workbook_id, ["Jan", "Feb"], append=True)
    assert status["status"] == "succeeded", status
    results = client.get(f"/api/jobs/{status['id']}/results").json()
    assert len(results["outputs"]) == 1
    merged = results["outputs"][0]
    assert merged["kind"] == "stacked"
    assert merged["tables"] == ["Jan", "Feb"]
    assert merged["rows"] == 20
    assert results["relationships"][0]["decision"] == "appended"
    # Raw extent is reported per sheet, alongside the cleaned size.
    jan = results["sheets"][0]
    assert jan["raw_rows"] >= jan["rows"] == 10


def test_append_off_keeps_sheets_separate(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    status = _run(client, workbook_id, ["Jan", "Feb"], append=False)
    results = client.get(f"/api/jobs/{status['id']}/results").json()
    assert [output["kind"] for output in results["outputs"]] == ["standalone", "standalone"]
    assert results["relationships"][0]["decision"] == "kept_separate"


def test_only_selected_sheets_are_cleaned(client):
    workbook_id = _upload(client, TWO_TABLES).json()["id"]
    status = _run(client, workbook_id, ["Producer"])
    results = client.get(f"/api/jobs/{status['id']}/results").json()
    assert [sheet["sheet"] for sheet in results["sheets"]] == ["Producer"]
    assert len(results["outputs"]) == 1


def test_header_rename_validation_and_export(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    status = _run(client, workbook_id, ["Jan", "Feb"])
    job_id = status["id"]
    output = client.get(f"/api/jobs/{job_id}/results").json()["outputs"][0]
    base = f"/api/jobs/{job_id}/outputs/{output['id']}"
    first, second = output["column_names"][1], output["column_names"][2]

    empty = client.put(f"{base}/headers", json={"renames": {first: "   "}})
    assert empty.status_code == 422
    assert empty.json()["error"]["message"] == "Column name cannot be empty."

    duplicate = client.put(f"{base}/headers", json={"renames": {first: second}})
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.json()["error"]["message"]

    saved = client.put(f"{base}/headers", json={"renames": {first: "Profit Center"}})
    assert saved.status_code == 200
    assert saved.json()["column_names"][1] == "Profit Center"

    csv_text = client.get(f"{base}/export/csv").text
    assert csv_text.splitlines()[0].split(",")[1] == "Profit Center"

    metadata = client.get(f"{base}/export/metadata").text
    assert "Profit Center" in metadata
    # Still described per source sheet after the rename.
    assert ",Jan," in metadata and ",Feb," in metadata

    preview = client.get(f"{base}/preview", params={"limit": 5, "sort": "Profit Center"}).json()
    assert preview["total"] == 20 and len(preview["rows"]) == 5

    zipped = client.get(f"/api/jobs/{job_id}/export/zip")
    assert zipped.status_code == 200


def test_sheet_with_no_table_fails_with_advice(client, tmp_path):
    path = tmp_path / "blank.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Empty"
    workbook.save(path)
    workbook_id = _upload(client, path).json()["id"]
    status = _run(client, workbook_id, ["Empty"])
    assert status["status"] == "failed"
    assert status["error"]["kind"] == "no_table"


# --------------------------------------------------------------------------- #
# Append outcome, reported after the run (no pre-run cleaning pass)
# --------------------------------------------------------------------------- #


def test_schema_check_endpoint_is_gone(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    response = client.post(f"/api/workbooks/{workbook_id}/schema-check", json={"sheets": ["Jan"]})
    assert response.status_code in (404, 405)


def test_append_report_when_all_sheets_match(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    status = _run(client, workbook_id, ["Jan", "Feb"], append=True)
    check = client.get(f"/api/jobs/{status['id']}/results").json()["append_check"]
    assert check["status"] == "all_match"
    assert check["groups"][0]["tables"] == ["Jan", "Feb"] and check["groups"][0]["appended"]


def test_append_report_explains_a_mismatch(client):
    workbook_id = _upload(client, TWO_TABLES).json()["id"]
    status = _run(client, workbook_id, ["Report", "Producer"], append=True)
    results = client.get(f"/api/jobs/{status['id']}/results").json()
    check = results["append_check"]
    assert check["status"] == "none_match"
    assert check["outputs"] == len(results["outputs"]) == 2
    other = next(group for group in check["groups"] if not group["is_reference"])
    assert other["missing_columns"] and other["extra_columns"]


def test_no_append_report_when_keeping_sheets_separate(client):
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    status = _run(client, workbook_id, ["Jan", "Feb"], append=False)
    assert client.get(f"/api/jobs/{status['id']}/results").json()["append_check"] is None


def test_each_sheet_is_read_and_cleaned_once(client, monkeypatch):
    from api.services import sheet_worker

    calls = {"read": [], "extract": 0}
    real_read, real_extract = sheet_worker.read_sheet, sheet_worker.extract_sheet

    def read(path, name, *args, **kwargs):
        calls["read"].append(name)
        return real_read(path, name, *args, **kwargs)

    def extract(*args, **kwargs):
        calls["extract"] += 1
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(sheet_worker, "read_sheet", read)
    monkeypatch.setattr(sheet_worker, "extract_sheet", extract)
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    _run(client, workbook_id, ["Jan", "Feb"])
    # One read per selected sheet -- no whole-workbook pass -- and one clean each.
    assert calls == {"read": ["Jan", "Feb"], "extract": 2}


# --------------------------------------------------------------------------- #
# Consistency checks per table
# --------------------------------------------------------------------------- #

BANNERED = LEGACY / "File2_Scenarios5-8_Combined.xlsx"
TRANSPOSED = LEGACY / "File4_Scenario_transposed.xlsx"


def _consistency(client, path, sheets):
    workbook_id = _upload(client, path).json()["id"]
    status = _run(client, workbook_id, sheets)
    results = client.get(f"/api/jobs/{status['id']}/results").json()
    output = results["outputs"][0]
    return results, results["consistency"][output["id"]]


def _check(report, check_id):
    return next(check for check in report["checks"] if check["id"] == check_id)


def test_clean_workbook_passes_every_check(client):
    results, report = _consistency(client, MULTI_SHEET, ["Jan", "Feb"])
    assert report["status"] == "passed" and report["issues"] == 0
    assert {check["id"] for check in report["checks"]} == {
        "row_accounting", "row_conservation", "total_reconciliation",
        "empty_key_column", "unexplained_empty_column", "duplicate_columns",
    }
    # Both appended sheets are accounted for, line by line.
    for sheet in report["accounting"]:
        assert sheet["raw"] == sheet["blank"] + sheet["header"] + sheet["removed"] + sheet["kept"]
    assert results["summary"]["consistency_issues"] == 0


def test_rows_outside_the_table_are_accounted_for(client):
    """File2: '#VALUE!' merged over A1:D2, then 'Exavalu' alone on row 3, above the table.

    Neither is table-shaped, so region finding rejects both. They must still leave with a
    reason: the error rows are not blank (they held a value), and the lone title is a banner.
    """
    results, report = _consistency(client, BANNERED, ["Data"])
    sheet = report["accounting"][0]
    # Only rows 4 and 19 are truly blank.
    assert sheet["raw"] == 21 and sheet["blank"] == 2 and sheet["header"] == 1
    assert sheet["removed"] == 8 and sheet["kept"] == 10 and sheet["unaccounted"] == 0
    assert sheet["removed_by_reason"] == {"EXCEL_ERROR": 2, "BANNER": 3, "GRAND_TOTAL": 1, "FOOTER": 2}
    removed = {row["sheet_row"]: row["classification"] for row in sheet["removed_rows"]}
    assert removed[1] == removed[2] == "EXCEL_ERROR" and removed[3] == "BANNER"
    assert _check(report, "row_accounting")["status"] == "passed"
    # Rows outside the region do not disturb the in-region conservation contract.
    assert _check(report, "row_conservation")["status"] == "passed"
    assert _check(report, "total_reconciliation")["status"] == "passed"
    assert report["status"] == "passed" and results["summary"]["consistency_issues"] == 0


def test_row_accounting_catches_a_silently_dropped_row():
    """If a row ever leaves without a logged reason, the sheet-level accounting says so."""
    from ahi_clean.extract import extract_sheet
    from ahi_clean.reader import read_workbook
    from api.services import consistency_service

    [grid] = read_workbook(BANNERED)
    results = extract_sheet(grid)
    trace = results[0].trace
    trace["dropped_rows"] = [row for row in trace["dropped_rows"] if row["sheet_row"] != 3]
    sheet = consistency_service._account(grid, results)
    assert sheet["unaccounted"] == 1 and sheet["status"] == "failed"
    assert sheet["unaccounted_rows"] == [{"sheet_row": 3, "content": "Exavalu"}]


def test_transposed_sheet_is_accounted_along_its_columns(client):
    _, report = _consistency(client, TRANSPOSED, ["Sheet"])
    sheet = report["accounting"][0]
    assert sheet["axis"] == "columns" and sheet["unaccounted"] == 0
    assert _check(report, "row_accounting")["status"] == "passed"


def test_checks_with_nothing_to_check_are_not_applicable(client):
    _, report = _consistency(client, MULTI_SHEET, ["Jan", "Feb"])
    # No grand total was removed, so there was nothing to reconcile.
    assert _check(report, "total_reconciliation")["status"] == "not_applicable"


def test_a_contract_violation_fails_its_check(client, monkeypatch):
    from ahi_clean import contracts

    real = contracts.check_all

    def with_violation(results):
        return real(results) + [contracts.Violation(results[0].label, contracts.EMPTY_KEY_COLUMN, "key column 'id' is entirely null")]

    monkeypatch.setattr(contracts, "check_all", with_violation)
    _, report = _consistency(client, MULTI_SHEET, ["Jan"])
    check = _check(report, "empty_key_column")
    assert check["status"] == "failed" and "entirely null" in check["details"][0]
    assert report["issues"] == 1


# --------------------------------------------------------------------------- #
# Batches: several files, each with its own sheets and append mode
# --------------------------------------------------------------------------- #


def _run_batch(client, files):
    response = client.post("/api/batches", json={"files": files})
    assert response.status_code == 202, response.text
    batch_id = response.json()["id"]
    for _ in range(600):
        status = client.get(f"/api/batches/{batch_id}").json()
        if status["status"] not in ("queued", "running"):
            return status
        time.sleep(0.05)
    raise AssertionError("batch did not finish")


def test_batch_cleans_each_file_with_its_own_settings(client):
    multi = _upload(client, MULTI_SHEET).json()["id"]
    two = _upload(client, TWO_TABLES).json()["id"]
    status = _run_batch(client, [
        {"workbook_id": multi, "sheets": ["Jan", "Feb"], "append": True},
        {"workbook_id": two, "sheets": ["Report", "Producer"], "append": False},
    ])
    assert status["status"] == "succeeded", status
    assert status["files_total"] == status["files_succeeded"] == 2 and status["progress"] == 1.0
    first, second = status["jobs"]
    assert first["batch_id"] == second["batch_id"] == status["id"]
    assert first["workbook_id"] == multi and second["workbook_id"] == two

    appended = client.get(f"/api/jobs/{first['id']}/results").json()
    assert [output["kind"] for output in appended["outputs"]] == ["stacked"]
    separate = client.get(f"/api/jobs/{second['id']}/results").json()
    assert [output["kind"] for output in separate["outputs"]] == ["standalone", "standalone"]
    assert separate["append_check"] is None


def test_batch_zip_has_one_folder_per_file(client):
    import zipfile

    first = _upload(client, MULTI_SHEET).json()["id"]
    # Same file name twice: the folders must not collide.
    second = _upload(client, MULTI_SHEET).json()["id"]
    status = _run_batch(client, [
        {"workbook_id": first, "sheets": ["Jan"], "append": True},
        {"workbook_id": second, "sheets": ["Feb"], "append": True},
    ])
    response = client.get(f"/api/batches/{status['id']}/export/zip")
    assert response.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    folders = {name.split("/")[0] for name in names}
    assert folders == {"File5_Scenario_MultiSheet", "File5_Scenario_MultiSheet (2)"}
    assert all(name.count("/") == 1 for name in names)


def test_one_failing_file_does_not_stop_the_batch(client, tmp_path):
    path = tmp_path / "blank.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Empty"
    workbook.save(path)
    blank = _upload(client, path).json()["id"]
    good = _upload(client, MULTI_SHEET).json()["id"]
    status = _run_batch(client, [
        {"workbook_id": blank, "sheets": ["Empty"], "append": True},
        {"workbook_id": good, "sheets": ["Jan", "Feb"], "append": True},
    ])
    assert status["status"] == "partial"
    assert [job["status"] for job in status["jobs"]] == ["failed", "succeeded"]
    assert status["jobs"][0]["error"]["kind"] == "no_table"


def test_batch_is_validated_before_anything_runs(client):
    good = _upload(client, MULTI_SHEET).json()["id"]
    other = _upload(client, TWO_TABLES).json()["id"]
    bad = client.post("/api/batches", json={"files": [
        {"workbook_id": good, "sheets": ["Jan"]},
        {"workbook_id": other, "sheets": ["Nope"]},
    ]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_sheet"
    # Nothing was started for the valid first file.
    from api.store import store
    assert not store.jobs_for(good)

    duplicate = client.post("/api/batches", json={"files": [
        {"workbook_id": good, "sheets": ["Jan"]},
        {"workbook_id": good, "sheets": ["Feb"]},
    ]})
    assert duplicate.status_code == 422
    assert client.post("/api/batches", json={"files": []}).status_code == 422


def test_cancelling_a_batch_skips_files_still_waiting(client, monkeypatch):
    import threading

    from api import config
    from api.services import sheet_worker

    started, release = threading.Event(), threading.Event()
    real_extract = sheet_worker.extract_sheet

    def slow_extract(*args, **kwargs):
        started.set()
        release.wait(5)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(sheet_worker, "extract_sheet", slow_extract)
    # One file at a time, so the second is still waiting when the batch is cancelled.
    monkeypatch.setattr(config, "BATCH_FILE_CONCURRENCY", 1)
    first = _upload(client, MULTI_SHEET).json()["id"]
    second = _upload(client, TWO_TABLES).json()["id"]
    batch_id = client.post("/api/batches", json={"files": [
        {"workbook_id": first, "sheets": ["Jan"]},
        {"workbook_id": second, "sheets": ["Producer"]},
    ]}).json()["id"]
    assert started.wait(5)
    # A file in a running batch can't be removed from under it.
    assert client.delete(f"/api/workbooks/{second}").status_code == 409
    cancelled = client.post(f"/api/batches/{batch_id}/cancel").json()
    assert cancelled["jobs"][1]["status"] == "cancelled"
    release.set()
    for _ in range(200):
        status = client.get(f"/api/batches/{batch_id}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert status["status"] == "cancelled"
    assert [job["status"] for job in status["jobs"]] == ["cancelled", "cancelled"]
    assert client.get(f"/api/batches/{batch_id}/export/zip").status_code == 409

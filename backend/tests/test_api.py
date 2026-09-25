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
    from api.services import cleaning_service

    calls = {"read": 0, "extract": 0}
    real_read, real_extract = cleaning_service.read_workbook, cleaning_service.extract_sheet

    def read(*args, **kwargs):
        calls["read"] += 1
        return real_read(*args, **kwargs)

    def extract(*args, **kwargs):
        calls["extract"] += 1
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(cleaning_service, "read_workbook", read)
    monkeypatch.setattr(cleaning_service, "extract_sheet", extract)
    workbook_id = _upload(client, MULTI_SHEET).json()["id"]
    _run(client, workbook_id, ["Jan", "Feb"])
    assert calls == {"read": 1, "extract": 2}


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


def test_row_accounting_catches_a_silently_dropped_row(client):
    """File2 row 3 ('Exavalu') sits above the table: neither kept nor logged as removed."""
    results, report = _consistency(client, BANNERED, ["Data"])
    sheet = report["accounting"][0]
    assert sheet["raw"] == 21 and sheet["blank"] == 4 and sheet["header"] == 1
    assert sheet["removed"] == 5 and sheet["kept"] == 10
    assert sheet["unaccounted"] == 1
    assert sheet["unaccounted_rows"] == [{"sheet_row": 3, "content": "Exavalu"}]
    assert _check(report, "row_accounting")["status"] == "failed"
    assert report["status"] == "failed" and results["summary"]["consistency_issues"] >= 1
    # The grand total it removed does reconcile, so that check genuinely passed.
    assert _check(report, "total_reconciliation")["status"] == "passed"


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

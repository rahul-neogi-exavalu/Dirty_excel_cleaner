"""The Validate step through the HTTP API, without a database: what the database would say
(the bronze column mapping, the control table) is stubbed."""

import io
import sys
import time
from datetime import date
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

HEADERS = ["AccountingEffectiveDate", "CommissionPct", "GrossCommissionAmount", "InsuranceCompany Name",
           "MarketProvider", "Pol_Ef_Dt", "PolicyNumber", "Premium", "Producer/Agency Name",
           "ProducerCommissionAmount", "ProducerCommissionPct", "Revenue", "Transaction Detail",
           "TransactionEffectiveDate"]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from api import config

    work = tmp_path_factory.mktemp("workspace")
    config.WORK_DIR = work
    config.UPLOAD_DIR = work / "uploads"
    config.JOB_DIR = work / "jobs"
    from api.main import app

    return TestClient(app)


@pytest.fixture
def context(monkeypatch):
    """The database's answers: one saved mapping for PC0101 (Pol_Ef_Dt is its PED), and
    whatever each test puts in ``loaded``."""
    from api import config
    from api.services import validation_service

    monkeypatch.setattr(config, "DB_CONFIGURED", True)
    monkeypatch.setattr(config, "INGEST_ENABLED", True)
    monkeypatch.setattr(config, "AI_ENABLED", False)
    monkeypatch.setattr(config, "WORD2VEC_PATH", "")
    found = validation_service.Context(
        divisions=[("AH Programs", "No", "0101")],
        mapping_rows=[("PC0101", "Pol_Ef_Dt", None, "policy_effective_date")],
    )
    monkeypatch.setattr(validation_service, "_read", lambda names, conn=None: found)
    return found


def _book(months, headers=HEADERS, blank_aed=False, ped=None):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Data"
    sheet.append(headers)
    n = 0
    for month in months:
        for day in (3, 17):
            n += 1
            row = {
                "AccountingEffectiveDate": None if blank_aed and n == 1 else date(2026, month, day),
                "CommissionPct": 0.1, "GrossCommissionAmount": 100.5 + n, "InsuranceCompany Name": "Harrier",
                "MarketProvider": "Market A", "Pol_Ef_Dt": ped or date(2026, month, 1), "PolicyNumber": f"POL-{n:04d}",
                "Premium": 1000 + n, "Producer/Agency Name": "Brown & Brown", "ProducerCommissionAmount": 50 + n,
                "ProducerCommissionPct": 0.05, "Revenue": 40 + n, "Transaction Detail": "New",
                "TransactionEffectiveDate": date(2026, month, day),
            }
            sheet.append([row.get(name) for name in headers])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _clean(client, name, content):
    upload = client.post("/api/workbooks", files={"file": (name, content)}).json()
    job = client.post("/api/jobs", json={"workbook_id": upload["id"], "sheets": [s["name"] for s in upload["sheets"]]}).json()
    for _ in range(300):
        status = client.get(f"/api/jobs/{job['id']}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert status["status"] == "succeeded", status
    return job["id"]


def _validate(client, name, content):
    response = client.post("/api/validations", json={"job_ids": [_clean(client, name, content)]})
    assert response.status_code == 201, response.text
    data = response.json()
    return data, data["outputs"][0]


def _required(output, name):
    return next(item for item in output["required"] if item["name"] == name)


def test_a_year_to_date_file_with_every_column_is_ready(client, context):
    data, output = _validate(client, "PC0101_Report_07132026.xlsx", _book(range(1, 7)))
    file = data["files"][0]
    assert (file["pc_id"], file["source_system"], file["file_received_date"]) == ("PC0101", "EXT_PC0101", "2026-07-13")
    assert file["division_name"] == "AH Programs"
    assert output["missing"] == [] and all(item["column"] for item in output["required"])
    # The bronze column mapping (seeded from the DRT mapping) knows Pol_Ef_Dt for PC0101.
    assert _required(output, "policy_effective_date")["vote"]["method"] == "saved"
    assert _required(output, "premium")["vote"]["method"] == "exact"
    assert (output["date_detail"], output["period_type"]) == ("AED", "YTD")
    assert (output["reporting_start_date"], output["reporting_end_date"]) == ("2026-01-01", "2026-06-30")
    assert output["action"] == "INSERT" and output["verdict"] == "ready" and output["fitness"] == 100
    assert data["counts"]["ready"] == 1


def test_one_empty_aed_hands_the_dates_to_ped_and_a_non_ytd_span_is_flagged(client, context):
    _, output = _validate(client, "PC0101_Partial_07132026.xlsx", _book(range(2, 7), blank_aed=True))
    assert output["dates"]["AED"]["complete"] is False and output["dates"]["PED"]["complete"] is True
    assert output["date_detail"] == "PED" and output["period_type"] is None
    assert output["verdict"] == "flagged" and output["action"] == "REJECTED" and output["flag"]


def test_the_reviewer_corrects_flagged_dates(client, context):
    data, output = _validate(client, "PC0101_FebJun_07132026.xlsx", _book(range(2, 7)))
    assert output["verdict"] == "flagged"
    fixed = client.patch(f"/api/validations/{data['id']}/outputs/{output['key']}",
                         json={"reporting_start_date": "2026-01", "reporting_end_date": "2026-06"}).json()["outputs"][0]
    assert fixed["verdict"] == "ready" and fixed["period_type"] == "YTD" and fixed["date_detail"] is None
    assert fixed["origin"] == "entered"


def test_a_missing_required_column_rejects_the_file(client, context):
    headers = [name for name in HEADERS if name != "Revenue"]
    data, output = _validate(client, "PC0101_NoRevenue_07132026.xlsx", _book(range(1, 7), headers=headers))
    assert output["missing"] == ["Revenue"] and output["verdict"] == "rejected" and output["action"] == "REJECTED"
    # Mapping another column to it would be the reviewer's call; clearing a found one rejects too.
    key = output["key"]
    cleared = client.patch(f"/api/validations/{data['id']}/outputs/{key}",
                           json={"mapping": {"premium": None}}).json()["outputs"][0]
    assert set(cleared["missing"]) == {"Revenue", "Premium"}
    assert _required(cleared, "premium")["reviewer"] is True


def test_a_month_already_in_bronze_is_the_reviewers_call(client, context):
    from ahi_bronze.validation import Loaded

    context.loaded = {"EXT_PC0101": [Loaded(5, "PC0101_H1_07132026.xlsx", "YTD", date(2026, 1, 1), date(2026, 6, 30),
                                            rows=120, columns=14, months={"2026-06": {"rows": 20, "AED": 100.0,
                                                                                      "PED": 100.0, "TED": 95.0}})]}
    data, output = _validate(client, "PC0101_June_07132026.xlsx", _book([6]))
    assert output["period_type"] == "MONTHLY" and output["action"] == "DECIDE"
    assert output["verdict"] == "needs_input" and output["options"] == ["replace_month", "reject"]
    compare = output["compare"]
    assert compare["month"] == "2026-06" and compare["earlier"][0]["rows"] == 20 and compare["this"]["rows"] == 2
    chosen = client.patch(f"/api/validations/{data['id']}/outputs/{output['key']}",
                          json={"choice": "replace_month"}).json()["outputs"][0]
    assert chosen["verdict"] == "ready" and chosen["action"] == "APPEND" and chosen["replace_month"] == "2026-06"
    assert chosen["file_replaced"] == "PC0101_H1_07132026.xlsx"
    # July after the YTD file is simply appended.
    _, july = _validate(client, "PC0101_July_08142026.xlsx", _book([7]))
    assert july["action"] == "APPEND" and july["verdict"] == "ready"


def test_no_profit_center_or_received_date_needs_input(client, context):
    data, output = _validate(client, "Report_without_codes.xlsx", _book(range(1, 7)))
    assert output["verdict"] == "needs_input" and data["files"][0]["pc_id"] is None
    job = data["files"][0]["job_id"]
    after = client.patch(f"/api/validations/{data['id']}/files/{job}", json={"pc_id": "101"}).json()
    assert after["files"][0]["pc_id"] == "PC0101"
    output = after["outputs"][0]
    assert output["verdict"] == "needs_input" and "Enter the file received date." in output["needs"]
    done = client.patch(f"/api/validations/{data['id']}/files/{job}", json={"file_received_date": "2026-07-13"}).json()
    assert done["outputs"][0]["verdict"] == "ready"
    bad = client.patch(f"/api/validations/{data['id']}/files/{job}", json={"pc_id": "abc"})
    assert bad.status_code == 422

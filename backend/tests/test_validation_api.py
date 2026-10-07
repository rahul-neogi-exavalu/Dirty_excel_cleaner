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


DATES = ("AccountingEffectiveDate", "Pol_Ef_Dt", "TransactionEffectiveDate")


def _fill(sheet, months, headers=HEADERS, blank=(), ped=None):
    """``blank``: the columns left empty on the first row."""
    sheet.append(headers)
    n = 0
    for month in months:
        for day in (3, 17):
            n += 1
            row = {
                "AccountingEffectiveDate": date(2026, month, day),
                "CommissionPct": 0.1, "GrossCommissionAmount": 100.5 + n, "InsuranceCompany Name": "Harrier",
                "MarketProvider": "Market A", "Pol_Ef_Dt": ped or date(2026, month, 1), "PolicyNumber": f"POL-{n:04d}",
                "Premium": 1000 + n, "Producer/Agency Name": "Brown & Brown", "ProducerCommissionAmount": 50 + n,
                "ProducerCommissionPct": 0.05, "Revenue": 40 + n, "Transaction Detail": "New",
                "TransactionEffectiveDate": date(2026, month, day), "Notes": f"note {n}",
            }
            if n == 1:
                row.update(dict.fromkeys(blank))
            sheet.append([row.get(name) for name in headers])


def _save(book):
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _book(months, headers=HEADERS, blank_aed=False, ped=None):
    book = openpyxl.Workbook()
    book.active.title = "Data"
    _fill(book.active, months, headers, ("AccountingEffectiveDate",) if blank_aed else (), ped)
    return _save(book)


def _sheets(*sheets):
    """A workbook of several sheets: (title, months, headers, blank) each. Different headers
    keep them apart (the cleaner stacks sheets whose headers are the same)."""
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, months, headers, blank in sheets:
        _fill(book.create_sheet(title), months, headers, blank)
    return _save(book)


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


def _by_sheet(data):
    return {output["sheet_name"]: output for output in data["outputs"]}


def _check(output, check_id):
    return next(check for check in output["checks"] if check["id"] == check_id)


def _patch(client, data, output, change):
    response = client.patch(f"/api/validations/{data['id']}/outputs/{output['key']}", json=change)
    assert response.status_code == 200, response.text
    return response.json()


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


def test_either_column_of_a_one_of_pair_is_enough(client, context):
    # CommissionPct without GrossCommissionAmount; ProducerCommissionAmount without ProducerCommissionPct.
    headers = [name for name in HEADERS if name not in ("GrossCommissionAmount", "ProducerCommissionPct")]
    _, output = _validate(client, "PC0101_OneOfEach_07132026.xlsx", _book(range(1, 7), headers=headers))
    assert output["missing"] == [] and output["verdict"] == "ready" and output["fitness"] == 100
    assert _required(output, "gross_commission_amount")["column"] is None
    assert _required(output, "commission_pct")["one_of"] == ["gross_commission_amount"]
    assert _required(output, "producer_commission_pct")["one_of"] == ["producer_commission_amount"]
    assert _required(output, "revenue")["one_of"] == []
    assert _check(output, "columns")["detail"] == "12 of 12 found"


def test_neither_column_of_a_one_of_pair_rejects_the_file(client, context):
    headers = [name for name in HEADERS if name not in ("CommissionPct", "GrossCommissionAmount")]
    _, output = _validate(client, "PC0101_NoCommission_07132026.xlsx", _book(range(1, 7), headers=headers))
    assert output["missing"] == ["CommissionPct or GrossCommissionAmount"]
    assert output["verdict"] == "rejected" and output["action"] == "REJECTED"
    assert "Missing required column: CommissionPct or GrossCommissionAmount." in output["reasons"]


def test_clearing_one_column_of_a_found_pair_leaves_the_file_fit(client, context):
    data, output = _validate(client, "PC0101_Both_07132026.xlsx", _book(range(1, 7)))
    assert output["verdict"] == "ready"
    cleared = _patch(client, data, output, {"mapping": {"commission_pct": None}})["outputs"][0]
    assert cleared["missing"] == [] and cleared["verdict"] == "ready"
    both = _patch(client, data, output, {"mapping": {"gross_commission_amount": None}})["outputs"][0]
    assert both["missing"] == ["CommissionPct or GrossCommissionAmount"] and both["verdict"] == "rejected"


def test_one_unfit_sheet_rejects_every_sheet_of_the_file(client, context):
    no_revenue = [name for name in HEADERS if name != "Revenue"]
    data, _ = _validate(client, "PC0101_TwoSheets_07132026.xlsx", _sheets(
        ("Fit", range(1, 7), HEADERS, ()), ("NoRevenue", range(1, 7), no_revenue, ())))
    sheets = _by_sheet(data)
    assert set(sheets) == {"Fit", "NoRevenue"}
    assert sheets["NoRevenue"]["missing"] == ["Revenue"] and sheets["NoRevenue"]["verdict"] == "rejected"
    fit = sheets["Fit"]
    assert fit["missing"] == [] and fit["verdict"] == "rejected" and fit["action"] == "REJECTED"
    assert "NoRevenue (Missing required column: Revenue)" in fit["reasons"][0]
    assert _check(fit, "file") == {"id": "file", "label": "Every sheet of the file", "ok": False,
                                   "detail": "Rejected with NoRevenue"}
    assert fit["fitness"] < 100 and data["counts"]["rejected"] == 2
    # Mapping a column to Revenue makes that sheet fit, and the file with it.
    premium = next(column["original"] for column in sheets["NoRevenue"]["columns"] if column["header"] == "Premium")
    after = _by_sheet(_patch(client, data, sheets["NoRevenue"], {"mapping": {"revenue": premium}}))
    assert {name: output["verdict"] for name, output in after.items()} == {"Fit": "ready", "NoRevenue": "ready"}
    assert _check(after["Fit"], "file") == {"id": "file", "label": "Every sheet of the file", "ok": True,
                                            "detail": "The other 1 sheet fit"}
    assert after["Fit"]["fitness"] == 100


def test_a_sheet_waiting_for_the_reviewer_holds_the_file_back(client, context):
    data, _ = _validate(client, "PC0101_Undated_07132026.xlsx", _sheets(
        ("Fit", range(1, 7), HEADERS, ()), ("Undated", range(1, 7), HEADERS + ["Notes"], DATES)))
    sheets = _by_sheet(data)
    assert sheets["Undated"]["verdict"] == "needs_input" and sheets["Undated"]["reporting_start_date"] is None
    fit = sheets["Fit"]
    assert fit["verdict"] == "needs_input" and fit["action"] == "INSERT"
    assert fit["needs"] == ["Finish Undated first: a file's sheets are staged together."]
    assert _check(fit, "file")["detail"] == "Waiting on Undated"
    after = _by_sheet(_patch(client, data, sheets["Undated"],
                             {"reporting_start_date": "2026-01", "reporting_end_date": "2026-06"}))
    assert after["Fit"]["verdict"] == "ready" and after["Undated"]["verdict"] == "ready"
    # The reviewer rejecting one sheet rejects the other.
    rejected = _by_sheet(_patch(client, data, after["Fit"], {"choice": "reject"}))
    assert rejected["Fit"]["reasons"] == ["Rejected by the reviewer."]
    assert rejected["Undated"]["verdict"] == "rejected" and "Fit (Rejected by the reviewer)" in rejected["Undated"]["reasons"][0]
    assert any("so are the file's other 1 sheet" in warning for warning in rejected["Fit"]["warnings"])


def test_staging_one_sheet_stages_the_whole_file(client, context, monkeypatch):
    from contextlib import contextmanager

    from api.services import validation_service

    class Conn:
        def execute(self, *args, **kwargs):
            return self

    @contextmanager
    def connection():
        yield Conn()

    staged = []

    def stage_one(conn, session, output, user_name):
        staged.append(output.key)
        return {"control_id": len(staged), "staging_table": f"stg_{len(staged)}",
                "processing_action": output.decision.action, "seeded": False}

    monkeypatch.setattr(validation_service.db, "connection", connection)
    monkeypatch.setattr(validation_service, "_stage_one", stage_one)
    data, _ = _validate(client, "PC0101_Both_07132026.xlsx", _sheets(
        ("Jan", range(1, 7), HEADERS, ()), ("Feb", range(1, 7), HEADERS + ["Notes"], ())))
    keys = [output["key"] for output in data["outputs"]]
    assert len(keys) == 2
    response = client.post(f"/api/validations/{data['id']}/stage", json={"keys": keys[:1]})
    assert response.status_code == 200, response.text
    assert sorted(staged) == sorted(keys) and response.json()["staged"] == 2

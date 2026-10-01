"""File -> Bronze end to end, through the HTTP API, against a real Postgres.

Skipped unless ``AHI_TEST_DATABASE_URL`` points at a database the test may write to,
e.g. ``postgresql://postgres:postgres@127.0.0.1:5432/postgres``. Each run uses its own
throwaway schemas and drops them afterwards.
"""

import io
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = os.environ.get("AHI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="AHI_TEST_DATABASE_URL is not set")

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient  # noqa: E402

BASE = ["profit_center_name", "policy_number", "premium", "accounting_effective_date"]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from psycopg.conninfo import conninfo_to_dict

    from api import config, db

    parts = conninfo_to_dict(URL)
    suffix = uuid.uuid4().hex[:8]
    work = tmp_path_factory.mktemp("workspace")
    saved = {name: getattr(config, name) for name in (
        "WORK_DIR", "UPLOAD_DIR", "JOB_DIR", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "DB_SSLMODE", "BRONZE_SCHEMA", "CONTROL_SCHEMA", "DB_CONFIGURED", "INGEST_ENABLED")}
    config.WORK_DIR, config.UPLOAD_DIR, config.JOB_DIR = work, work / "uploads", work / "jobs"
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), "disable"
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_t{suffix}", f"ingest_t{suffix}"
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    db.close()
    from api.main import app

    yield TestClient(app), config
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA):
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for name, value in saved.items():
        setattr(config, name, value)


def _workbook(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, (header, rows) in sheets.items():
        sheet = book.create_sheet(name)
        sheet.append(header)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _rows(months: list[int], header: list[str], per_month: int = 4) -> list[list]:
    rows, n = [], 0
    for month in months:
        for _ in range(per_month):
            n += 1
            values = {
                "profit_center_name": f"Zone {n % 3} PC",
                "policy_number": f"POL-{month:02d}{n:04d}",
                "premium": 1000.5 + n,
                "accounting_effective_date": date(2025, month, 1 + n % 27),
                "commission": 0.1 * n,
                "agency": f"Agency {n % 4}",
                "carrier": f"Carrier {n % 5}",
            }
            rows.append([values[name] for name in header])
    return rows


def _clean(client, filename: str, content: bytes) -> str:
    upload = client.post("/api/workbooks", files={"file": (filename, content)}).json()
    job = client.post("/api/jobs", json={"workbook_id": upload["id"], "sheets": [s["name"] for s in upload["sheets"]]}).json()
    for _ in range(300):
        status = client.get(f"/api/jobs/{job['id']}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert status["status"] == "succeeded", status
    return job["id"]


def _ingest(client, job_id: str, confirm: bool = True, expect_actions=None) -> dict:
    plan = client.post("/api/bronze/plans", json={"job_ids": [job_id]})
    assert plan.status_code == 201, plan.text
    plan = plan.json()
    if expect_actions is not None:
        assert [item["action"] for item in plan["items"]] == expect_actions, plan["items"]
    keys = [item["key"] for item in plan["items"]] if confirm else []
    approved = client.post(f"/api/bronze/plans/{plan['id']}/approve", json={"reviewed_by": "Test Reviewer", "confirmed": keys})
    if approved.status_code != 202:
        return {"approve": approved}
    for _ in range(300):
        state = client.get(f"/api/bronze/plans/{plan['id']}").json()
        if state["status"] not in ("draft", "running"):
            break
        time.sleep(0.1)
    assert state["status"] == "succeeded", state
    return state


def _query(config, statement: str):
    with psycopg.connect(URL) as conn:
        return conn.execute(statement.format(b=config.BRONZE_SCHEMA, c=config.CONTROL_SCHEMA)).fetchall()


def _columns(config, table: str) -> list[str]:
    return [row[0] for row in _query(config, (
        "SELECT column_name FROM information_schema.columns WHERE table_schema = '{b}' "
        f"AND table_name = '{table}' AND column_name NOT LIKE '\\_%' ORDER BY ordinal_position"))]


def test_scenario_document_end_to_end(env):
    client, config = env
    assert client.get("/api/bronze/status").json()["reachable"] is True
    h1 = list(range(1, 7))

    # Jan-Jun, single sheet -> CREATE ext_pc0515_arr
    first = _clean(client, "ARR_pc0515.xlsx", _workbook({"ARR": (BASE, _rows(h1, BASE))}))
    _ingest(client, first, expect_actions=["create"])
    assert _columns(config, "ext_pc0515_arr") == BASE
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr")[0][0] == 24

    # Case 1: July, identical schema -> APPEND
    _ingest(client, _clean(client, "ARR_jul_pc0515.xlsx", _workbook({"ARR": (BASE, _rows([7], BASE))})),
            expect_actions=["append"])
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr")[0][0] == 28

    # Case 2: same columns, other order -> REORDER; the table keeps its order
    reordered = list(reversed(BASE))
    plan_job = _clean(client, "ARR_jul2_pc0515.xlsx", _workbook({"ARR": (reordered, _rows([8], reordered))}))
    _ingest(client, plan_job, expect_actions=["reorder"])
    assert _columns(config, "ext_pc0515_arr") == BASE
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr WHERE policy_number LIKE 'POL-08%'")[0][0] == 4

    # Case 3: an extra column -> EVOLVE, which must be confirmed
    wider = BASE + ["commission"]
    evolve_job = _clean(client, "ARR_sep_pc0515.xlsx", _workbook({"ARR": (wider, _rows([9], wider))}))
    refused = _ingest(client, evolve_job, confirm=False, expect_actions=["evolve"])
    assert refused["approve"].status_code == 409
    _ingest(client, evolve_job, expect_actions=["evolve"])
    assert _columns(config, "ext_pc0515_arr") == wider
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr WHERE commission IS NULL")[0][0] == 32

    # Case 4: a different schema -> its own table
    other = ["agency", "carrier", "accounting_effective_date"]
    _ingest(client, _clean(client, "ARR_oct_pc0515.xlsx", _workbook({"ARR": (other, _rows([10], other))})),
            expect_actions=["new_table"])
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr_2025_10")[0][0] == 4

    # Revised Jan-Sep file -> REPLACE every earlier load of the table (rebuild)
    revised = _clean(client, "ARR_rev_pc0515.xlsx", _workbook({"ARR": (BASE, _rows(list(range(1, 10)), BASE, 2))}))
    _ingest(client, revised, expect_actions=["replace"])
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0515_arr")[0][0] == 18
    assert _columns(config, "ext_pc0515_arr") == BASE
    statuses = dict(_query(config, "SELECT status, count(*) FROM {c}.ingestion WHERE table_name = 'ext_pc0515_arr' GROUP BY status"))
    assert statuses == {"ingested": 1, "superseded": 4}

    # Month-named sheets appended into one table -> ext_<src>_data
    content = _workbook({"Jan": (BASE, _rows([1], BASE)), "Feb": (BASE, _rows([2], BASE))})
    monthly = _clean(client, "Prem_pc0094.xlsx", content)
    # Renaming the provenance column must not lose the row lineage.
    output = client.get(f"/api/jobs/{monthly}/results").json()["outputs"][0]["id"]
    renamed = client.put(f"/api/jobs/{monthly}/outputs/{output}/headers", json={"renames": {"source_sheet": "Month Sheet"}})
    assert renamed.status_code == 200, renamed.text
    _ingest(client, monthly, expect_actions=["create"])
    assert _query(config, "SELECT count(DISTINCT _source_sheet) FROM {b}.ext_pc0094_data")[0][0] == 2
    assert "month_sheet" in _columns(config, "ext_pc0094_data")

    # The very same file (same bytes) again -> SKIP
    again = _clean(client, "Prem_pc0094.xlsx", content)
    plan = client.post("/api/bronze/plans", json={"job_ids": [again]}).json()
    assert [item["action"] for item in plan["items"]] == ["skip"]

    tables = {table["table_name"]: table for table in client.get("/api/bronze/tables").json()}
    assert tables["ext_pc0515_arr"]["rows"] == 18
    assert (tables["ext_pc0515_arr"]["period_start"], tables["ext_pc0515_arr"]["period_end"]) == ("2025-01", "2025-09")

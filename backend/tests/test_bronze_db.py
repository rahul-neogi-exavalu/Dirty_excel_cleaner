"""File -> Validate -> staging -> Bronze end to end, through the HTTP API, against a real Postgres.

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
from sse_client import validate  # noqa: E402

# Every required column, under the business's DRT names -- except the policy effective
# date, which this profit center writes its own way (the mapping learns it).
HEADERS = ["AccountingEffective Date", "CommissionPct", "GrossCommissionAmount", "InsuranceCompany Name",
           "MarketProvider", "Pol_Ef_Dt", "PolicyNumber", "Premium", "Producer/Agency Name",
           "ProducerCommissionAmount", "ProducerCommissionPct", "Revenue", "Transaction Detail",
           "TransactionEffectiveDate"]
# Every bronze table carries these besides the file's own columns.
SYSTEM = ["pc_id", "file_received_date", "reporting_start_date", "reporting_end_date", "division_name", "file_name",
          "processing_date"]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from psycopg.conninfo import conninfo_to_dict

    from api import config, db

    parts = conninfo_to_dict(URL)
    suffix = uuid.uuid4().hex[:8]
    work = tmp_path_factory.mktemp("workspace")
    names = ("WORK_DIR", "UPLOAD_DIR", "JOB_DIR", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
             "DB_SSLMODE", "BRONZE_SCHEMA", "CONTROL_SCHEMA", "SILVER_SCHEMA", "CLEANSED_SCHEMA", "STAGING_SCHEMA",
             "DB_CONFIGURED", "INGEST_ENABLED", "AI_ENABLED", "WORD2VEC_PATH")
    saved = {name: getattr(config, name) for name in names}
    config.WORK_DIR, config.UPLOAD_DIR, config.JOB_DIR = work, work / "uploads", work / "jobs"
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), parts.get("sslmode", "disable")
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_t{suffix}", f"ingest_t{suffix}"
    config.SILVER_SCHEMA, config.CLEANSED_SCHEMA = f"silver_t{suffix}", f"cleansed_t{suffix}"
    config.STAGING_SCHEMA = f"staging_t{suffix}"
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    config.AI_ENABLED, config.WORD2VEC_PATH = False, ""  # deterministic: no external calls
    db.close()
    from api.main import app

    yield TestClient(app), config
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA, config.CLEANSED_SCHEMA,
                       config.STAGING_SCHEMA):
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for name, value in saved.items():
        setattr(config, name, value)


def _rows(months, per_month: int = 4, start: int = 0, blank_aed: bool = False) -> list[list]:
    rows, n = [], start
    for month in months:
        for index in range(per_month):
            n += 1
            values = {
                "AccountingEffective Date": None if blank_aed and index == 0 else date(2026, month, 1 + n % 27),
                "CommissionPct": 0.12, "GrossCommissionAmount": 120.5 + n, "InsuranceCompany Name": f"Carrier {n % 5}",
                "MarketProvider": "Admitted", "Pol_Ef_Dt": date(2026, month, 1), "PolicyNumber": f"POL-{month:02d}{n:04d}",
                "Premium": 1000.5 + n, "Producer/Agency Name": f"Agency {n % 4}", "ProducerCommissionAmount": 60 + n,
                "ProducerCommissionPct": 0.06, "Revenue": 60.5, "Transaction Detail": "New Business",
                "TransactionEffectiveDate": date(2026, month, 1 + n % 27),
            }
            rows.append(values)
    return rows


def _workbook(rows: list[dict], headers=HEADERS, sheet: str = "ARR") -> bytes:
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append([row.get(name) for name in headers])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


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


def _validate(client, filename: str, content: bytes) -> tuple[dict, dict]:
    session = validate(client, [_clean(client, filename, content)])
    return session, session["outputs"][0]


def _change(client, session: dict, output: dict, **change) -> tuple[dict, dict]:
    response = client.patch(f"/api/validations/{session['id']}/outputs/{output['key']}", json=change)
    assert response.status_code == 200, response.text
    session = response.json()
    return session, next(item for item in session["outputs"] if item["key"] == output["key"])


def _stage(client, session: dict) -> dict:
    response = client.post(f"/api/validations/{session['id']}/stage", json={})
    assert response.status_code == 200, response.text
    return response.json()["outputs"][0]["staged"]


def _ingest(client, expect_actions=None) -> dict:
    """Plan every staged file, confirm everything, load."""
    plan = client.post("/api/bronze/plans", json={})
    assert plan.status_code == 201, plan.text
    plan = plan.json()
    if expect_actions is not None:
        assert [item["action"] for item in plan["items"]] == expect_actions, plan["items"]
    keys = [item["key"] for item in plan["items"]]
    approved = client.post(f"/api/bronze/plans/{plan['id']}/approve", json={"confirmed": keys})
    assert approved.status_code == 202, approved.text
    for _ in range(300):
        state = client.get(f"/api/bronze/plans/{plan['id']}").json()
        if state["status"] not in ("draft", "running"):
            break
        time.sleep(0.1)
    assert state["status"] == "succeeded", state
    return state


def _query(config, statement: str, *args):
    with psycopg.connect(URL) as conn:
        return conn.execute(statement.format(b=config.BRONZE_SCHEMA, c=config.CONTROL_SCHEMA, s=config.STAGING_SCHEMA),
                            args).fetchall()


def _columns(config, table: str, schema: str | None = None) -> list[str]:
    return [row[0] for row in _query(config, (
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"), schema or config.BRONZE_SCHEMA, table)]


def _control(config, control_id: int) -> dict:
    names = ("control_id", "source_system", "file_name", "reporting_period_type", "processing_action",
             "bronze_load_flag", "file_received_date", "drt_reporting_start_date", "drt_reporting_end_date",
             "date_detail", "file_replaced", "is_active", "ingestion_id", "staging_table", "rejection_reason",
             "replace_month")
    row = _query(config, f"SELECT {', '.join(names)} FROM {{c}}.control_table WHERE control_id = %s", control_id)[0]
    return dict(zip(names, row))


def _column_of(output: dict, header: str) -> str:
    return next(column["original"] for column in output["columns"] if column["header"] == header)


def test_validate_stage_and_load(env):
    client, config = env
    assert client.get("/api/bronze/status").json()["reachable"] is True

    # The business's control table is there, every row waiting (N), numbered on from 73.
    seeded = _query(config, "SELECT count(*), count(*) FILTER (WHERE bronze_load_flag = 'N'), max(control_id) "
                            "FROM {c}.control_table")[0]
    assert seeded == (73, 73, 73)
    # The bronze column mapping holds the DRT mapping's rows for the required columns.
    drt = _query(config, "SELECT count(*), count(DISTINCT silver_column_name) FROM {b}.bronze_column_mapping")[0]
    assert drt[0] > 100 and drt[1] <= 14

    # 1. Jan-Jun, every AED present: year to date, INSERT. Pol_Ef_Dt is PC0001's own
    #    name for the policy effective date: the reviewer maps it, and the mapping learns it.
    session, output = _validate(client, "PC0001_ARR Jan-Jun_07132026.xlsx", _workbook(_rows(range(1, 7))))
    assert session["files"][0]["file_received_date"] == "2026-07-13"
    if not next(item for item in output["required"] if item["name"] == "policy_effective_date")["column"]:
        session, output = _change(client, session, output,
                                  mapping={"policy_effective_date": _column_of(output, "Pol_Ef_Dt")})
    assert output["missing"] == [] and output["verdict"] == "ready"
    assert (output["date_detail"], output["period_type"], output["action"]) == ("AED", "YTD", "INSERT")
    staged = _stage(client, session)
    assert staged["control_id"] == 74 and staged["processing_action"] == "INSERT"
    row = _control(config, 74)
    assert (row["source_system"], row["bronze_load_flag"], row["is_active"]) == ("EXT_PC0001", "N", "Y")
    assert (row["file_received_date"], row["drt_reporting_start_date"], row["drt_reporting_end_date"]) == (
        date(2026, 7, 13), date(2026, 1, 1), date(2026, 6, 30))
    # Staged under the bronze table it is bound for.
    assert staged["staging_table"] == "ext_pc0001_arr_stg"
    assert _query(config, f'SELECT count(*) FROM {{s}}."{staged["staging_table"]}"')[0][0] == 24
    assert _query(config, "SELECT method FROM {b}.bronze_column_mapping WHERE profit_center = 'PC0001' "
                          "AND file_columns = 'Pol_Ef_Dt' AND silver_column_name = 'policy_effective_date'")
    _ingest(client, expect_actions=["create"])
    loaded = _control(config, 74)
    assert loaded["bronze_load_flag"] == "Y" and loaded["ingestion_id"]
    # The business's layout: pc_id, the file's 14 columns, the file-level values, then lineage.
    columns = _columns(config, "ext_pc0001_arr")
    lineage = ["_ingestion_id", "_source_file", "_source_sheet", "_reporting_month", "_ingested_at"]
    assert columns[0] == "pc_id" and columns[-len(SYSTEM) - 4:] == SYSTEM[1:] + lineage
    assert len(columns) == 1 + len(HEADERS) + len(SYSTEM) - 1 + len(lineage)
    assert _query(config, "SELECT count(DISTINCT _reporting_month), min(reporting_start_date)::text, "
                          "max(reporting_end_date)::text FROM {b}.ext_pc0001_arr")[0] == (6, "2026-01-01", "2026-06-30")
    audit = _query(config, "SELECT control_id, reporting_period_type, file_received_date::text FROM {c}.ingestion "
                           "WHERE status = 'ingested'")
    assert audit == [(74, "YTD", "2026-07-13")]

    # 2. July only: monthly, appended. The mapping now knows Pol_Ef_Dt.
    session, output = _validate(client, "PC0001_ARR July_08142026.xlsx", _workbook(_rows([7], start=100)))
    assert next(item for item in output["required"] if item["name"] == "policy_effective_date")["vote"]["method"] == "saved"
    assert (output["period_type"], output["action"], output["verdict"]) == ("MONTHLY", "APPEND", "ready")
    july = _stage(client, session)["control_id"]
    _ingest(client, expect_actions=["append"])

    # 3. June again: a month already in Bronze. The reviewer sees both sides, replaces June.
    session, output = _validate(client, "PC0001_ARR June revised_08202026.xlsx", _workbook(_rows([6], start=200)))
    assert output["action"] == "DECIDE" and output["compare"]["earlier"][0]["rows"] == 4
    session, output = _change(client, session, output, choice="replace_month")
    assert (output["action"], output["replace_month"], output["verdict"]) == ("APPEND", "2026-06", "ready")
    june = _stage(client, session)["control_id"]
    plan = client.post("/api/bronze/plans", json={}).json()
    item = plan["items"][0]
    assert item["replace_month"] == "2026-06" and [ref["id"] for ref in item["month_replaces"]] == [loaded["ingestion_id"]]
    assert item["requires_confirmation"]
    _ingest(client, expect_actions=["append"])
    by_load = dict(_query(config, "SELECT _reporting_month || '|' || _ingestion_id, count(*) FROM {b}.ext_pc0001_arr "
                                  "GROUP BY 1"))
    assert f"2026-06|{loaded['ingestion_id']}" not in by_load  # the first file's June is gone
    assert by_load[f"2026-05|{loaded['ingestion_id']}"] == 4  # its other months stay
    assert _query(config, "SELECT rows_loaded, silver_status FROM {c}.ingestion WHERE id = %s",
                  loaded["ingestion_id"]) == [(20, None)]
    assert _control(config, 74)["is_active"] == "Y" and _control(config, june)["file_replaced"].startswith("PC0001_ARR Jan-Jun")

    # 4. Jan-Aug year to date: replaces every file of the year it covers.
    session, output = _validate(client, "PC0001_ARR Jan-Aug_09102026.xlsx", _workbook(_rows(range(1, 9), start=300)))
    assert output["action"] == "INSERT" and output["confirm"]
    ytd = _stage(client, session)["control_id"]
    _ingest(client, expect_actions=["replace"])
    assert [_control(config, cid)["is_active"] for cid in (74, july, june)] == ["N", "N", "N"]
    assert _control(config, ytd)["bronze_load_flag"] == "Y"
    assert _query(config, "SELECT count(*) FROM {b}.ext_pc0001_arr")[0][0] == 32
    assert _query(config, "SELECT count(*) FROM {c}.ingestion WHERE status = 'superseded'")[0][0] == 3

    # 5. Mar-Jun is neither year to date nor monthly: left as it is, it is rejected.
    session, output = _validate(client, "PC0002_Report Mar-Jun_07132026.xlsx", _workbook(_rows(range(3, 7))))
    assert output["verdict"] == "flagged" and output["flag"]
    rejected = _stage(client, session)
    row = _control(config, rejected["control_id"])
    assert (row["processing_action"], row["is_active"], row["bronze_load_flag"]) == ("REJECTED", "N", "N")
    assert "neither year-to-date" in row["rejection_reason"]

    # 6. A required column missing: rejected, never loaded.
    headers = [name for name in HEADERS if name != "Revenue"]
    session, output = _validate(client, "PC0003_NoRevenue_07132026.xlsx", _workbook(_rows(range(1, 7)), headers))
    assert output["missing"] == ["Revenue"] and output["verdict"] == "rejected"
    rejected = _stage(client, session)
    assert _control(config, rejected["control_id"])["rejection_reason"] == "Missing required column: Revenue."
    assert client.post("/api/bronze/plans", json={}).status_code == 409  # nothing staged to load

    # 7. A file the business already listed (control row 12): its row is filled in.
    name = "PC0796_2026-06 796 TPI - AJG Data Submission_796 TPI_06302026.xlsx"
    session, output = _validate(client, name, _workbook(_rows(range(1, 7), start=400)))
    assert output["control"] == {"control_id": 12, "seeded": True}
    file = session["files"][0]
    assert file["file_received_date"] == "2026-03-05"  # the control table's, over the name's 2026-06-30
    assert any("control table lists" in warning for warning in file["warnings"])
    if output["verdict"] != "ready":
        session, output = _change(client, session, output,
                                  mapping={"policy_effective_date": _column_of(output, "Pol_Ef_Dt")})
    assert _stage(client, session)["control_id"] == 12
    assert _query(config, "SELECT count(*) FROM {c}.control_table WHERE file_name = %s", name)[0][0] == 1
    _ingest(client, expect_actions=["create"])
    row = _control(config, 12)
    assert (row["bronze_load_flag"], row["date_detail"], row["reporting_period_type"]) == ("Y", "AED", "YTD")
    assert _query(config, "SELECT DISTINCT pc_id, division_name FROM {b}.ext_pc0796_arr") == [
        ("PC0796", "Bridge Specialty Group")]
    # Every control row the tests did not touch is still waiting.
    assert _query(config, "SELECT count(*) FROM {c}.control_table WHERE control_id <= 73 AND bronze_load_flag = 'N'")[0][0] == 72


def test_existing_bronze_tables_get_the_new_columns(env):
    """A table made before the control table: file_date renamed, reporting dates added, once."""
    client, config = env
    from api import db
    from api.services import bronze_service

    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f'CREATE TABLE "{config.BRONZE_SCHEMA}".ext_old_arr (pc_id text, premium text, file_date date, '
                     "_ingestion_id text, _source_file text, _source_sheet text, _ingested_at timestamptz)")
        conn.execute(f'INSERT INTO "{config.BRONZE_SCHEMA}".ext_old_arr VALUES (%s, %s, %s, %s, %s, %s, now())',
                     ["PC0009", "10", date(2026, 6, 1), "old", "old.xlsx", "ARR"])
        conn.execute(f'INSERT INTO "{config.CONTROL_SCHEMA}".bronze_table (table_name, schema_name, source_system, columns) '
                     "VALUES ('ext_old_arr', %s, 'pc0009', '[]')", [config.BRONZE_SCHEMA])
    with db.connection() as conn:
        assert bronze_service.upgrade_tables(conn) == ["ext_old_arr"]
        assert bronze_service.upgrade_tables(conn) == []  # nothing the second time
    columns = _columns(config, "ext_old_arr")
    assert "file_date" not in columns and {"file_received_date", "reporting_start_date", "reporting_end_date",
                                          "_reporting_month"} <= set(columns)
    assert _query(config, "SELECT file_received_date::text FROM {b}.ext_old_arr") == [("2026-06-01",)]

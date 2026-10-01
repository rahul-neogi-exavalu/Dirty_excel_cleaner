"""The 10 test workbooks, clean -> bronze -> silver, through the HTTP API, on Postgres.

Skipped unless AHI_TEST_DATABASE_URL points at a scratch database. Uses its own
throwaway schemas and drops them afterwards. The files come from
backend/tools/make_silver_test_files.py (enhancements/test-files/).
"""

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FILES = ROOT.parent / "enhancements" / "test-files"

URL = os.environ.get("AHI_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL or not FILES.exists(), reason="AHI_TEST_DATABASE_URL not set or test files missing")

psycopg = pytest.importorskip("psycopg")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from psycopg.conninfo import conninfo_to_dict

    from api import config, db

    parts = conninfo_to_dict(URL)
    suffix = uuid.uuid4().hex[:8]
    work = tmp_path_factory.mktemp("workspace")
    names = ("WORK_DIR", "UPLOAD_DIR", "JOB_DIR", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
             "DB_SSLMODE", "BRONZE_SCHEMA", "CONTROL_SCHEMA", "SILVER_SCHEMA", "LOTL_TABLE", "DB_CONFIGURED",
             "INGEST_ENABLED", "AI_ENABLED", "WORD2VEC_PATH")
    saved = {name: getattr(config, name) for name in names}
    config.WORK_DIR, config.UPLOAD_DIR, config.JOB_DIR = work, work / "uploads", work / "jobs"
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), "disable"
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_s{suffix}", f"ingest_s{suffix}"
    config.SILVER_SCHEMA, config.LOTL_TABLE = f"silver_s{suffix}", f"ingest_s{suffix}.lotl"
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    config.AI_ENABLED, config.WORD2VEC_PATH = False, ""  # deterministic: no external calls
    db.close()
    from api.main import app

    yield TestClient(app), config
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA):
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    for name, value in saved.items():
        setattr(config, name, value)


def _wait(client, url, active=("queued", "running", "draft")):
    for _ in range(600):
        state = client.get(url).json()
        if state["status"] not in active:
            return state
        time.sleep(0.1)
    raise AssertionError(f"{url} did not finish")


def _bronze(client, filename: str) -> list[str]:
    """Clean and ingest one test file, confirming everything; returns its table names."""
    with open(FILES / filename, "rb") as handle:
        upload = client.post("/api/workbooks", files={"file": (filename, handle.read())}).json()
    sheets = [s["name"] for s in upload["sheets"] if not s["hidden"]]
    job = client.post("/api/jobs", json={"workbook_id": upload["id"], "sheets": sheets}).json()
    assert _wait(client, f"/api/jobs/{job['id']}", ("queued", "running"))["status"] == "succeeded"
    plan = client.post("/api/bronze/plans", json={"job_ids": [job["id"]]}).json()
    keys = [item["key"] for item in plan["items"]]
    approved = client.post(f"/api/bronze/plans/{plan['id']}/approve", json={"reviewed_by": "Test Reviewer", "confirmed": keys})
    assert approved.status_code == 202, approved.text
    assert _wait(client, f"/api/bronze/plans/{plan['id']}")["status"] == "succeeded"
    return [item["table_name"] for item in plan["items"]]


def _silver_run(client, file_names: list[str]) -> dict:
    loads = client.get("/api/silver/eligible").json()["loads"]
    ids = [load["ingestion_id"] for load in loads if load["file_name"] in file_names]
    response = client.post("/api/silver/runs", json={"ingestion_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()


def _decide(client, run: dict, decisions: dict[str, str | None]) -> dict:
    """decisions: bronze column -> Silver column, or None for Ignore."""
    for table in run["tables"]:
        for row in table["mapping"]:
            if row["bronze_column"] in decisions:
                target = decisions[row["bronze_column"]]
                run = client.patch(f"/api/silver/runs/{run['id']}/mapping", json={
                    "table_name": table["table_name"], "bronze_column": row["bronze_column"],
                    "silver_column": target, "ignored": target is None}).json()
    return run


def _approve(client, run: dict) -> dict:
    response = client.post(f"/api/silver/runs/{run['id']}/approve", json={"reviewed_by": "Test Reviewer"})
    assert response.status_code == 202, response.text
    state = _wait(client, f"/api/silver/runs/{run['id']}")
    assert state["status"] == "succeeded", state
    return state


def _q(config, statement: str, *args):
    with psycopg.connect(URL) as conn:
        return conn.execute(statement.format(s=config.SILVER_SCHEMA, c=config.CONTROL_SCHEMA), args).fetchall()


def _methods(run: dict) -> dict[str, list[str]]:
    """Bronze column -> the methods that voted for its pre-selected Silver column."""
    return {row["bronze_column"]: row["methods"] for table in run["tables"] for row in table["mapping"]}


def test_ten_files_through_silver(env):
    client, config = env
    from tools.seed_lotl import seed

    # 1. Base load: every column matches exactly; nothing is saved before approval.
    _bronze(client, "01_ARR_pc0101_2026_JanJun.xlsx")
    seed(FILES / "lotl_seed.csv")
    run = _silver_run(client, ["01_ARR_pc0101_2026_JanJun.xlsx"])
    assert all("exact" in methods for methods in _methods(run).values()) and not run["blockers"]
    rows = [row for table in run["tables"] for row in table["mapping"]]
    assert {row["selection"] for row in rows} == {"recommended"}
    assert all(row["candidates"][0]["recommended"] for row in rows)
    assert client.get("/api/silver/mapping").json() == []
    _approve(client, run)
    assert len(client.get("/api/silver/mapping").json()) == 9
    assert _q(config, "SELECT count(*) FROM {s}.detail")[0][0] == 30

    # 2-3. July (identical) and August (reordered): the whole mapping is pre-filled from
    # the saved rows -- and still needs approval.
    for name in ("02_ARR_pc0101_2026_Jul.xlsx", "03_ARR_pc0101_2026_Aug_reordered.xlsx"):
        _bronze(client, name)
        run = _silver_run(client, [name])
        assert all(methods[0] == "saved" for methods in _methods(run).values()) and run["status"] == "draft"
        _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE pc_id = '0101'")[0][0] == 40

    # 4. Renamed + extra columns land in their own bronze table; Silver unifies them.
    tables = _bronze(client, "04_ARR_pc0101_2026_Sep_newcols.xlsx")
    assert tables == ["ext_pc0101_arr_2026_09"]
    run = _silver_run(client, ["04_ARR_pc0101_2026_Sep_newcols.xlsx"])
    methods = _methods(run)
    assert methods["premium_amt"] == ["fuzzy"] and "saved" in methods["profit_center_name"]
    assert {"carrier", "writing_agency", "notes"} <= {c for c, m in methods.items() if not m}
    refused = client.post(f"/api/silver/runs/{run['id']}/approve", json={"reviewed_by": "Test Reviewer"})
    assert refused.status_code == 409  # undecided columns block approval
    run = _decide(client, run, {"carrier": "insurance_company_name", "writing_agency": "producer_name", "notes": None})
    edited = {row["bronze_column"]: row for table in run["tables"] for row in table["mapping"]}
    assert edited["carrier"]["selection"] == "manual" and edited["premium_amt"]["selection"] == "recommended"
    state = _approve(client, run)
    snapshot = _q(config, "SELECT mapping FROM {c}.silver_run WHERE id = %s", state["id"])[0][0]
    audit = {row["bronze_column_name"]: row for row in snapshot}
    assert audit["premium_amt"]["methods"] == ["fuzzy"] and audit["notes"]["selection"] == "manual"
    assert {"method": "fuzzy", "silver_column": "premium"}.items() <= audit["premium_amt"]["votes"][0].items()
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE _bronze_table = 'ext_pc0101_arr_2026_09' "
                      "AND insurance_company_name IS NOT NULL")[0][0] == 5

    # 5. A second source with its own wording, unified into the same columns.
    _bronze(client, "05_Prem_pc0202_2026.xlsx")
    run = _silver_run(client, ["05_Prem_pc0202_2026.xlsx"])
    methods = _methods(run)
    # acct_eff_date reads as the already-approved accounting_effective_date: the saved
    # mapping, exact and fuzzy all vote for it, independently.
    assert methods["acct_eff_date"] == ["saved", "exact", "fuzzy"] and methods["written_premium"] == ["fuzzy"]
    run = _decide(client, run, {"carrier_name": "insurance_company_name", "agency": "producer_name",
                                "policy": "policy_number", "source_sheet": None})
    _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE pc_id = '0202' AND policy_number IS NOT NULL")[0][0] == 24

    # 6. The four profit-center cases against the seeded LOTL.
    _bronze(client, "06_PC_pc0303_2026_cases.xlsx")
    _approve(client, _silver_run(client, ["06_PC_pc0303_2026_cases.xlsx"]))
    rows = dict(_q(config, "SELECT policy_number, profit_center_number || '|' || coalesce(profit_center_name, '') "
                           "|| '|' || pc_lookup_status FROM {s}.detail WHERE pc_id = '0303'"))
    statuses = sorted(value.split("|")[2] for value in rows.values())
    assert statuses == ["corrected", "filled_name", "filled_number", "kept", "kept", "no_match"]
    assert "0094|Dayton Office|corrected" in rows.values()
    assert "0303|Columbus Office|filled_name" in rows.values()

    # 7-8. Date and money formats.
    for name in ("07_Dates_pc0404_2026.xlsx", "08_Money_pc0505_2026.xlsx"):
        _bronze(client, name)
        _approve(client, _silver_run(client, [name]))
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE pc_id = '0404' AND accounting_effective_date IS NULL")[0][0] == 2
    premiums = sorted(float(v[0]) for v in _q(config, "SELECT premium FROM {s}.detail WHERE pc_id = '0505' AND premium IS NOT NULL"))
    assert -250.0 in premiums and 1200.5 in premiums and len(premiums) == 7  # "abc" -> NULL
    producers = [r[0] for r in _q(config, "SELECT producer_name FROM {s}.detail WHERE pc_id = '0505'")]
    assert None in producers and all(p is None or p == p.strip() for p in producers)

    # 9. Revised Jan-Jun: bronze replaces file 1; Silver removes its rows on the next run.
    _bronze(client, "09_ARR_pc0101_2026_JanJun_revised.xlsx")
    # A run with no loads selected still removes the replaced rows.
    cleanup_only = client.post("/api/silver/runs", json={"ingestion_ids": []}).json()
    assert [item["file_name"] for item in cleanup_only["cleanup"]] == ["01_ARR_pc0101_2026_JanJun.xlsx"]
    assert _approve(client, cleanup_only)["result"]["rows_removed"] == 30
    # Two reviews of the same file: once one is loaded, the other is stale and refused.
    first = _silver_run(client, ["09_ARR_pc0101_2026_JanJun_revised.xlsx"])
    second = _silver_run(client, ["09_ARR_pc0101_2026_JanJun_revised.xlsx"])
    _approve(client, second)
    stale = client.post(f"/api/silver/runs/{first['id']}/approve", json={"reviewed_by": "Test Reviewer"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "plan_changed"
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE pc_id = '0101'")[0][0] == 24 + 5 + 5 + 5

    # 10. A dirty report: the fact table loads, the side lookup table is left unprocessed.
    tables = _bronze(client, "10_Report_pc0606_2026_JanJul.xlsx")
    assert len(tables) == 2
    loads = client.get("/api/silver/eligible").json()["loads"]
    fact = [l["ingestion_id"] for l in loads if l["table_name"] == "ext_pc0606_report"]
    run = client.post("/api/silver/runs", json={"ingestion_ids": fact}).json()
    _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.detail WHERE pc_id = '0606'")[0][0] == 21

    # The summary agrees with the detail, and the audit table records every run.
    detail = dict(_q(config, "SELECT pc_id, count(*) FROM {s}.detail GROUP BY 1"))
    summary = dict(_q(config, "SELECT pc_id, sum(row_count) FROM {s}.summary GROUP BY 1"))
    assert detail == {k: int(v) for k, v in summary.items()}
    assert _q(config, "SELECT count(*) FROM {c}.silver_run WHERE status = 'succeeded'")[0][0] == 11

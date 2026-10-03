"""The 14 test workbooks, clean -> bronze -> silver, through the HTTP API, on Postgres.

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
             "DB_SSLMODE", "BRONZE_SCHEMA", "CONTROL_SCHEMA", "SILVER_SCHEMA", "CLEANSED_SCHEMA", "LOTL_TABLE",
             "DB_CONFIGURED",
             "INGEST_ENABLED", "AI_ENABLED", "WORD2VEC_PATH")
    saved = {name: getattr(config, name) for name in names}
    config.WORK_DIR, config.UPLOAD_DIR, config.JOB_DIR = work, work / "uploads", work / "jobs"
    config.DB_HOST, config.DB_PORT = parts.get("host", "localhost"), int(parts.get("port", 5432))
    config.DB_NAME, config.DB_USER = parts.get("dbname", "postgres"), parts.get("user", "postgres")
    config.DB_PASSWORD, config.DB_SSLMODE = parts.get("password", ""), "disable"
    config.BRONZE_SCHEMA, config.CONTROL_SCHEMA = f"bronze_s{suffix}", f"ingest_s{suffix}"
    config.SILVER_SCHEMA, config.LOTL_TABLE = f"silver_s{suffix}", f"ingest_s{suffix}.lotl"
    config.CLEANSED_SCHEMA = f"cleansed_s{suffix}"
    config.DB_CONFIGURED = config.INGEST_ENABLED = True
    config.AI_ENABLED, config.WORD2VEC_PATH = False, ""  # deterministic: no external calls
    db.close()
    from api.main import app

    yield TestClient(app), config
    db.close()
    with psycopg.connect(URL, autocommit=True) as conn:
        for schema in (config.BRONZE_SCHEMA, config.CONTROL_SCHEMA, config.SILVER_SCHEMA, config.CLEANSED_SCHEMA):
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
    plan = _plan(client, filename)
    keys = [item["key"] for item in plan["items"]]
    approved = client.post(f"/api/bronze/plans/{plan['id']}/approve", json={"reviewed_by": "Test Reviewer", "confirmed": keys})
    assert approved.status_code == 202, approved.text
    assert _wait(client, f"/api/bronze/plans/{plan['id']}")["status"] == "succeeded"
    return [item["table_name"] for item in plan["items"]]


def _plan(client, filename: str) -> dict:
    """Clean one test file and plan its ingestion (file date filled in when the name has none)."""
    with open(FILES / filename, "rb") as handle:
        upload = client.post("/api/workbooks", files={"file": (filename, handle.read())}).json()
    sheets = [s["name"] for s in upload["sheets"] if not s["hidden"]]
    job = client.post("/api/jobs", json={"workbook_id": upload["id"], "sheets": sheets}).json()
    assert _wait(client, f"/api/jobs/{job['id']}", ("queued", "running"))["status"] == "succeeded"
    plan = client.post("/api/bronze/plans", json={"job_ids": [job["id"]]}).json()
    # Most test names carry a year but no month: the reviewer enters the file date.
    for file in plan["files"]:
        if not file["file_date"]:
            response = client.patch(f"/api/bronze/plans/{plan['id']}/files/{file['job_id']}", json={"file_date": "2026-06"})
            assert response.status_code == 200, response.text
            plan = response.json()
    assert all(file["pc_id"] for file in plan["files"]), plan["files"]
    return plan


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
        return conn.execute(statement.format(s=config.SILVER_SCHEMA, c=config.CONTROL_SCHEMA,
                                             cl=config.CLEANSED_SCHEMA), args).fetchall()


def _methods(run: dict) -> dict[str, list[str]]:
    """Bronze column -> the methods that voted for its pre-selected Silver column."""
    return {row["bronze_column"]: row["methods"] for table in run["tables"] for row in table["mapping"]}


def _rows_of(run: dict) -> dict[str, dict]:
    return {row["bronze_column"]: row for table in run["tables"] for row in table["mapping"]}


def _columns(config, table: str) -> list[str]:
    return [r[0] for r in _q(config, "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                                     "AND table_name = %s ORDER BY ordinal_position", config.SILVER_SCHEMA, table)]


def test_ten_files_through_silver(env):
    client, config = env
    from ahi_silver import catalog

    from tools.seed_reference import add_lotl

    # 1. Base load. The reference tables are filled from assets/ the first time the
    # database is used; the test offices are added to the LOTL on top.
    _bronze(client, "01_ARR_pc0101_2026_JanJun.xlsx")
    assert _q(config, "SELECT count(*) FROM {c}.division_mapping")[0][0] == 193
    lotl_rows = _q(config, "SELECT count(*) FROM {c}.lotl")[0][0]
    assert lotl_rows == 293  # exactly the workbook's rows
    exact = _q(config, "SELECT profit_center_number, legacy_office_name, status FROM {c}.lotl "
                       "WHERE profit_center_number = 'null' OR legacy_office_name LIKE '%%' || chr(10) || '%%'")
    assert sorted(exact) == sorted([("null", "ALTRU", "Active"), ("null", "Meridian", "Active"), ("null", "null", "null"),
                                    ("360", "N/A do not show\nCorp-Accession (Bridge)", "Active"),
                                    ("346", "N/A do not show\nCorp-Batta", "Active")])
    add_lotl(FILES / "lotl_seed.csv")
    run = _silver_run(client, ["01_ARR_pc0101_2026_JanJun.xlsx"])
    rows = _rows_of(run)
    assert not run["blockers"] and {row["selection"] for row in rows.values()} == {"recommended"}
    assert all(row["methods"] for row in rows.values()) and run["tables"][0]["pc_id"] == "PC0101"
    assert run["lotl_rows"] == lotl_rows + 10
    # The DRT mapping is the business's (660 rows); nothing for PC0101 until approval.
    mapping = client.get("/api/silver/mapping").json()
    assert len(mapping) >= 650 and not [r for r in mapping if r["profit_center"] == "PC0101"]
    state = _approve(client, run)
    saved = [r for r in client.get("/api/silver/mapping").json() if r["profit_center"] == "PC0101"]
    assert len(saved) == 9 and all(r["silver_column_name"] for r in saved)
    assert state["result"]["mappings_saved"] == 9
    # Saved under the headers the file wrote, with only the business's DRT labels.
    from api.services import reference_service

    labels = set(reference_service.drt_labels_from_file())
    assert {r["pc_column"] for r in saved} == {"Profit Center Name", "Profit Center Number", "Insurance Company Name",
                                               "Producer Name", "Policy Number", "Premium", "Accounting Effective Date",
                                               "Policy Effective Date", "Transaction Effective Date"}
    assert all(r["drt_column"] is None or r["drt_column"] in labels for r in saved)
    assert {r["drt_column"] for r in saved if r["pc_column"] == "Premium"} == {"Premium"}
    assert {r["drt_column"] for r in saved if r["pc_column"] == "Profit Center Name"} == {None}
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail")[0][0] == 30
    # Bronze -> Silver Cleansed (its own schema, the bronze table's name) -> Final Silver.
    assert _q(config, "SELECT count(*) FROM {cl}.ext_pc0101_arr")[0][0] == 30
    assert _q(config, "SELECT bronze_table, cleansed_table FROM {c}.silver_cleansed_table") == [
        ("ext_pc0101_arr", f"{config.CLEANSED_SCHEMA}.ext_pc0101_arr")]
    assert _q(config, "SELECT status FROM {c}.silver_draft WHERE id = %s", state["id"]) == [("succeeded",)]
    # Exactly the business's columns, in order.
    assert _columns(config, "silver_detail") == [c.name for c in catalog.load(config.SILVER_COLUMNS_FILE)]
    assert _columns(config, "silver_aggregate") == [c.name for c in catalog.load_plain(config.SILVER_AGGREGATE_COLUMNS_FILE)]
    # A load's rows carry its identity: bronze table, file and processing_date.
    identity = _q(config, "SELECT DISTINCT source_table, source_file, ingestion_timestamp FROM {s}.silver_detail")
    load = _q(config, "SELECT table_name, file_name, processing_date FROM {c}.ingestion WHERE status = 'ingested'")[0]
    assert identity == [(f"{config.BRONZE_SCHEMA}.{load[0]}", load[1], load[2])]

    # 2-3. July (identical) and August (reordered): the whole mapping is pre-filled from
    # the saved rows -- and still needs approval.
    for name in ("02_ARR_pc0101_2026_Jul.xlsx", "03_ARR_pc0101_2026_Aug_reordered.xlsx"):
        _bronze(client, name)
        run = _silver_run(client, [name])
        assert all(methods[0] == "saved" for methods in _methods(run).values()) and run["status"] == "draft"
        _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_system = 'pc0101'")[0][0] == 40

    # 4. Renamed + extra columns land in their own bronze table; Silver unifies them.
    tables = _bronze(client, "04_ARR_pc0101_2026_Sep_newcols.xlsx")
    assert tables == ["ext_pc0101_arr_v2"]
    run = _silver_run(client, ["04_ARR_pc0101_2026_Sep_newcols.xlsx"])
    # The review lives in the database: it survives the API letting go of every connection.
    from api import db

    db.close()
    assert client.get(f"/api/silver/runs/{run['id']}").json()["status"] == "draft"
    methods = _methods(run)
    assert "fuzzy" in methods["premium_amt"] and methods["profit_center_name"][0] == "saved"
    # "Carrier" is approved for other profit centers in the DRT mapping: a saved vote.
    assert "saved" in methods["carrier"] and _rows_of(run)["carrier"]["silver_column"] == "insurance_company_name"
    assert {"writing_agency", "notes"} <= {c for c, m in methods.items() if not m}
    refused = client.post(f"/api/silver/runs/{run['id']}/approve", json={"reviewed_by": "Test Reviewer"})
    assert refused.status_code == 409  # undecided columns block approval
    run = _decide(client, run, {"writing_agency": "producer_agency_name", "notes": None})
    edited = _rows_of(run)
    assert edited["writing_agency"]["selection"] == "manual" and edited["premium_amt"]["selection"] == "recommended"
    state = _approve(client, run)
    snapshot = _q(config, "SELECT mapping FROM {c}.silver_run WHERE id = %s", state["id"])[0][0]
    audit = {row["bronze_column_name"]: row for row in snapshot}
    assert "fuzzy" in audit["premium_amt"]["methods"] and audit["notes"]["selection"] == "manual"
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_table LIKE '%%.ext_pc0101_arr_v2' "
                      "AND insurance_company_name IS NOT NULL")[0][0] == 5
    # An ignored column is not saved: it is asked about again next time.
    mapping = client.get("/api/silver/mapping").json()
    assert not [r for r in mapping if r["silver_column_name"] is None and r["drt_column"] is None]
    assert not [r for r in mapping if r["profit_center"] == "PC0101" and r["pc_column"].casefold() == "notes"]

    # 5. A second source with its own wording, unified into the same columns.
    _bronze(client, "05_Prem_pc0202_2026.xlsx")
    run = _silver_run(client, ["05_Prem_pc0202_2026.xlsx"])
    methods = _methods(run)
    assert {"saved", "exact"} <= set(methods["acct_eff_date"]) and "fuzzy" in methods["written_premium"]
    run = _decide(client, run, {"carrier_name": "insurance_company_name", "agency": "producer_agency_name",
                                "policy": "policy_number", "source_sheet": None})
    _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_system = 'pc0202' "
                      "AND policy_number IS NOT NULL")[0][0] == 24

    # 6. The four profit-center cases against the LOTL.
    _bronze(client, "06_PC_pc0303_2026_cases.xlsx")
    state = _approve(client, _silver_run(client, ["06_PC_pc0303_2026_cases.xlsx"]))
    statuses = next(iter(state["result"]["quality"].values()))["profit_center"]
    assert statuses == {"kept": 2, "filled_number": 1, "corrected": 1, "filled_name": 1, "no_match": 1}
    values = [r[0] for r in _q(config, "SELECT profit_center_number || '|' || coalesce(profit_center_name, '') "
                                       "FROM {s}.silver_detail WHERE source_system = 'pc0303'")]
    assert "0094|Dayton Office" in values and "0303|Columbus Office" in values

    # 7-8. Date and money formats.
    for name in ("07_Dates_pc0404_2026.xlsx", "08_Money_pc0505_2026.xlsx"):
        _bronze(client, name)
        _approve(client, _silver_run(client, [name]))
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_system = 'pc0404' "
                      "AND accounting_effective_date IS NULL")[0][0] == 2
    premiums = sorted(float(v[0]) for v in _q(config, "SELECT premium FROM {s}.silver_detail "
                                                      "WHERE source_system = 'pc0505' AND premium IS NOT NULL"))
    assert -250.0 in premiums and 1200.5 in premiums and len(premiums) == 7  # "abc" -> NULL
    producers = [r[0] for r in _q(config, "SELECT producer_agency_name FROM {s}.silver_detail WHERE source_system = 'pc0505'")]
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
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_system = 'pc0101'")[0][0] == 24 + 5 + 5 + 5

    # 10. A dirty report: the fact table loads, the side lookup table is left unprocessed.
    tables = _bronze(client, "10_Report_pc0606_2026_JanJul.xlsx")
    assert len(tables) == 2
    loads = client.get("/api/silver/eligible").json()["loads"]
    # Both tables of the one sheet reached bronze (neither was taken for the other).
    assert {l["table_name"] for l in loads if l["file_name"] == "10_Report_pc0606_2026_JanJul.xlsx"} == set(tables)
    fact = [l["ingestion_id"] for l in loads if l["table_name"] == "ext_pc0606_report"]
    run = client.post("/api/silver/runs", json={"ingestion_ids": fact}).json()
    _approve(client, run)
    assert _q(config, "SELECT count(*) FROM {s}.silver_detail WHERE source_system = 'pc0606'")[0][0] == 21

    # The aggregate agrees with the detail, and the audit table records every run.
    detail = dict(_q(config, "SELECT source_system, sum(premium) FROM {s}.silver_detail GROUP BY 1"))
    aggregate = dict(_q(config, "SELECT source_system, sum(premium) FROM {s}.silver_aggregate GROUP BY 1"))
    assert detail == aggregate
    grain = _q(config, "SELECT DISTINCT record_grain, reporting_period_type FROM {s}.silver_aggregate "
                       "WHERE reporting_period IS NOT NULL")
    assert grain == [("PROFIT_CENTER_MONTH", "MONTH")]
    assert _q(config, "SELECT count(*) FROM {s}.silver_aggregate WHERE source_system = 'pc0101' "
                      "AND file_date IS NULL")[0][0] == 0
    rows = client.get("/api/silver/aggregate").json()
    assert rows and {"premium", "policy_count", "reporting_period"} <= set(rows[0])
    assert _q(config, "SELECT count(*) FROM {c}.silver_run WHERE status = 'succeeded'")[0][0] == 11


def test_audit_scenarios_11_to_14(env):
    """Run after the ten files: pc0101's table holds the revised Jan-Jun, July and August."""
    client, config = env
    with psycopg.connect(URL) as conn:
        bronze = config.BRONZE_SCHEMA

        def q(statement):
            return conn.execute(statement.format(b=bronze)).fetchall()

        # 11. May-Jul overlaps the loaded Jan-Jun only partly: nothing runs until the reviewer chooses.
        plan = _plan(client, "11_ARR_pc0101_2026_MayJul_overlap.xlsx")
        item = plan["items"][0]
        assert any("only partly" in blocker for blocker in item["blockers"])
        assert plan["files"][0]["period_start"] == "2026-05" and plan["files"][0]["period_end"] == "2026-07"
        refused = client.post(f"/api/bronze/plans/{plan['id']}/approve",
                              json={"reviewed_by": "Test Reviewer", "confirmed": [item["key"]]})
        assert refused.status_code == 409

        # 12. Reordered columns plus a new one: evolve the same table, keeping its order.
        before = [r[0] for r in q("SELECT column_name FROM information_schema.columns WHERE table_schema = '{b}' "
                                  "AND table_name = 'ext_pc0101_arr' ORDER BY ordinal_position")]
        plan = _plan(client, "12_ARR_pc0101_2026_Oct_reorder_newcol.xlsx")
        assert [(i["action"], i["table_name"]) for i in plan["items"]] == [("evolve", "ext_pc0101_arr")]
        assert plan["items"][0]["comparison"]["reordered"]
        assert _bronze(client, "12_ARR_pc0101_2026_Oct_reorder_newcol.xlsx") == ["ext_pc0101_arr"]
        after = [r[0] for r in q("SELECT column_name FROM information_schema.columns WHERE table_schema = '{b}' "
                                 "AND table_name = 'ext_pc0101_arr' ORDER BY ordinal_position")]
        assert "commission" in after and [c for c in after if c in before] == before

        # 13. Amounts the document's list does not cover, read exactly and strictly.
        _bronze(client, "13_MoneyDates_pc0909_2026.xlsx")
        _approve(client, _silver_run(client, ["13_MoneyDates_pc0909_2026.xlsx"]))
        premiums = sorted((r[0] for r in _q(config, "SELECT premium FROM {s}.silver_detail WHERE source_system = 'pc0909'")),
                          key=lambda v: (v is None, v))
        from decimal import Decimal

        assert premiums == [Decimal("-250.00"), Decimal("0.13"), Decimal("1.01"), Decimal("9999999999999999.99"),
                            None, None]
        # Each cleansed row says which values could not be read.
        unreadable = _q(config, "SELECT count(*) FROM {cl}.ext_pc0909_edge WHERE 'premium' = ANY(_invalid_columns)")
        assert unreadable == [(2,)]

        # 14. Duplicate headers listed the other way round still land in their own columns.
        _bronze(client, "14_Clash_pc0707_2026_Jan.xlsx")
        plan = _plan(client, "14_Clash_pc0707_2026_Feb_swapped.xlsx")
        assert plan["items"][0]["column_map"] == {"amount": "amount_2", "amount_2": "amount"}
        _bronze(client, "14_Clash_pc0707_2026_Feb_swapped.xlsx")
        dollars = q("SELECT amount::numeric FROM {b}.ext_pc0707_clash ORDER BY policy_number")
        assert all(row[0] >= 100 for row in dollars) and len(dollars) == 8

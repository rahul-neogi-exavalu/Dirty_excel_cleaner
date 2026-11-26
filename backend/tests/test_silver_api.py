"""The Silver catalog the UI reads, through the HTTP API (no database needed)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from api.main import app

    return TestClient(app)


def test_the_catalog_marks_the_drt_columns_the_dropdowns_offer(client):
    catalog = client.get("/api/silver/catalog").json()
    columns = {column["name"]: column for column in catalog["columns"]}
    targets = [column for column in catalog["columns"] if column["target"]]
    assert len(catalog["columns"]) == 69 and len(targets) == 48
    assert all(column["role"] == "mapped" and column["drt_name"] for column in targets)
    assert columns["policy_tran_id"]["target"] and columns["policy_tran_id"]["drt_name"] == "Policy Transaction ID"
    # In the Silver table, but not DRT columns: never offered.
    assert not columns["UltimateParentProducerNames"]["target"] and not columns["ajg_apd"]["target"]
    assert not columns["row_hash"]["target"]


def test_a_rule_is_validated_before_it_is_saved(client):
    good = {"profit_center": "2041", "key": "pc2041.flag", "kind": "constant", "column": "renewal_flag",
            "spec": {"value": True}}
    assert client.post("/api/silver/rules/validate", json=good).json() == {"valid": True, "errors": []}
    bad = {**good, "column": "record_grain", "spec": {}}
    result = client.post("/api/silver/rules/validate", json=bad).json()
    assert not result["valid"]
    assert any("needs 'value'" in error for error in result["errors"])
    assert any("not a transaction column" in error for error in result["errors"])


def test_a_rule_spec_converts_to_sql_and_back_through_the_api(client):
    spec = {"expr": {"op": "mul", "args": ["src:Premium", 0.1]}}
    shown = client.post("/api/silver/rules/to-sql", json={"kind": "derive", "spec": spec}).json()
    assert shown == {"sql": 'src."Premium" * 0.1', "reason": None}
    back = client.post("/api/silver/rules/from-sql",
                       json={"kind": "derive", "sql": shown["sql"], "spec": {**spec, "mode": "fill_null"}}).json()
    assert back == {"spec": {**spec, "mode": "fill_null"}, "error": None}

    lookup = client.post("/api/silver/rules/to-sql", json={"kind": "lookup", "spec": {}}).json()
    assert lookup["sql"] is None and "edited as JSON" in lookup["reason"]
    broken = client.post("/api/silver/rules/from-sql", json={"kind": "derive", "sql": "UPPER(a"}).json()
    assert broken["spec"] is None and broken["error"]["start"] == 7

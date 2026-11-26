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

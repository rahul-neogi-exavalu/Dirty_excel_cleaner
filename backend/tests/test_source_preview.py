"""The source preview: an uploaded file's sheets as they are, before cleaning.

Builds its workbooks in memory, so it pins behaviour rather than a sample file.
"""

import datetime as dt
import io
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from api import config

    work = tmp_path_factory.mktemp("workspace")
    config.WORK_DIR = work
    config.UPLOAD_DIR = work / "uploads"
    config.JOB_DIR = work / "jobs"
    from api.main import app

    return TestClient(app)


def _book() -> bytes:
    book = openpyxl.Workbook()
    report = book.active
    report.title = "Report"
    # A banner over two blank rows, then a table with a gap row in it.
    report["A1"] = "Quarterly premium report"
    report.merge_cells("A1:D1")
    report.append([])
    report.append(["Policy", "Premium", "Effective", "Note"])
    report.append(["  POL-1  ", 1083.0, dt.datetime(2026, 1, 15), "#VALUE!"])
    report.append([])
    report.append(["POL-2", 250, dt.datetime(2026, 2, 1, 9, 30), None])
    wide = book.create_sheet("Wide")
    wide.append([f"c{index}" for index in range(1, 131)])
    for row in range(2, 46):
        wide.append([row * 1000 + index for index in range(1, 131)])
    book.create_sheet("Blank")
    hidden = book.create_sheet("Lookup")
    hidden.sheet_state = "hidden"
    hidden["B2"] = "only cell"
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def workbook(client):
    response = client.post("/api/workbooks", files={"file": ("report.xlsx", _book())})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _preview(client, workbook_id, sheet, **params):
    return client.get(f"/api/workbooks/{workbook_id}/preview", params={"sheet": sheet, **params})


def test_cells_keep_their_sheet_positions_and_raw_values(client, workbook):
    data = _preview(client, workbook, "Report", limit=10).json()
    assert (data["total_rows"], data["total_columns"], data["available_rows"]) == (6, 4, 6)
    assert data["columns"] == ["A", "B", "C", "D"]
    rows = data["rows"]
    # Row 1 is the banner; its merged range holds the value in the top-left cell only.
    assert rows[0] == ["Quarterly premium report", None, None, None]
    assert data["merged"] == ["A1:D1"] and data["merged_total"] == 1
    # Blank rows stay, so row numbers match Excel's.
    assert rows[1] == [None, None, None, None] and rows[4] == [None, None, None, None]
    assert rows[2] == ["Policy", "Premium", "Effective", "Note"]
    # Untrimmed text, the error text itself, a date as Excel shows it.
    assert rows[3] == ["  POL-1  ", 1083, "2026-01-15", "#VALUE!"]
    assert rows[5] == ["POL-2", 250, "2026-02-01 09:30:00", None]
    assert data["source_format"] == "xlsx" and data["hidden"] is False


def test_rows_and_columns_page_independently(client, workbook):
    data = _preview(client, workbook, "Wide", offset=20, limit=10, col_offset=100, col_limit=50).json()
    assert (data["total_rows"], data["total_columns"]) == (45, 130)
    # Columns CW (101st) onwards: the window stops at the last used column, DZ (130th).
    assert data["columns"][0] == "CW" and data["columns"][-1] == "DZ" and len(data["columns"]) == 30
    assert len(data["rows"]) == 10
    # Sheet row 21 is the 21st row: value row*1000 + column.
    assert data["rows"][0][0] == 21 * 1000 + 101

    past = _preview(client, workbook, "Wide", offset=45, limit=10).json()
    assert past["rows"] == []


def test_page_limits_are_enforced(client, workbook):
    assert _preview(client, workbook, "Wide", limit=101).status_code == 422
    assert _preview(client, workbook, "Wide", col_limit=101).status_code == 422
    assert _preview(client, workbook, "Wide", col_limit=100).json()["columns"][-1] == "CV"


def test_empty_hidden_and_unknown_sheets(client, workbook):
    blank = _preview(client, workbook, "Blank").json()
    assert (blank["total_rows"], blank["total_columns"], blank["rows"], blank["columns"]) == (0, 0, [], [])

    hidden = _preview(client, workbook, "Lookup").json()
    assert hidden["hidden"] is True
    assert hidden["rows"] == [[None, None], [None, "only cell"]]

    missing = _preview(client, workbook, "Nope")
    assert missing.status_code == 404
    assert _preview(client, "no-such-upload", "Report").status_code == 404


def test_a_large_sheet_pages_through_its_first_rows(client, monkeypatch):
    from api import config
    from api.services import source_preview

    monkeypatch.setattr(config, "SOURCE_PREVIEW_MAX_CELLS", 400)
    upload = client.post("/api/workbooks", files={"file": ("big.xlsx", _book())}).json()["id"]
    data = _preview(client, upload, "Wide", limit=100).json()
    # 130 cells a row: held up to the ceiling, the rest only counted.
    assert data["total_rows"] == 45 and data["available_rows"] == 4
    assert len(data["rows"]) == 4
    source_preview.forget(upload)


def test_delimited_file_is_one_sheet_of_text(client):
    content = "id;name;amount\n1;Alpha;10,5\n\n2;Beta;\n".encode("utf-8")
    upload = client.post("/api/workbooks", files={"file": ("ledger.csv", content)}).json()
    data = _preview(client, upload["id"], upload["sheets"][0]["name"]).json()
    assert data["source_format"] == "Semicolon-separated text, UTF-8"
    assert data["rows"] == [["id", "name", "amount"], ["1", "Alpha", "10,5"], [None, None, None], ["2", "Beta", None]]


def test_removing_the_file_forgets_its_preview(client):
    from api.services import source_preview

    upload = client.post("/api/workbooks", files={"file": ("gone.xlsx", _book())}).json()["id"]
    assert _preview(client, upload, "Report").status_code == 200
    assert any(key[0] == upload for key in source_preview._cache)
    assert client.delete(f"/api/workbooks/{upload}").status_code == 204
    assert not any(key[0] == upload for key in source_preview._cache)
    assert _preview(client, upload, "Report").status_code == 404

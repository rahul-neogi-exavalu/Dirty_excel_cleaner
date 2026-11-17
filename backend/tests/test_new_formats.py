"""Legacy .xls and .txt inputs, beside the .xlsx and .csv the cleaner started with.

The behaviour is pinned on the real sender files in ``Sample_Formats/`` (skipped when the
folder is absent) plus a few files built in place for the cases a sample cannot show.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from ahi_clean import failures, reader  # noqa: E402
from ahi_clean.extract import extract_sheet  # noqa: E402

SAMPLES = ROOT.parent / "Sample_Formats"
OLE = bytes.fromhex("d0cf11e0a1b11ae1")
ZIP = b"PK" + bytes([3, 4])


def sample(pattern: str) -> Path:
    found = sorted(SAMPLES.glob(pattern))
    if not found:
        pytest.skip(f"no {pattern} in Sample_Formats")
    return found[0]


# --- which reader opens a file ---------------------------------------------------------

def test_the_first_bytes_beat_the_extension(tmp_path):
    zipped = tmp_path / "renamed.xls"
    zipped.write_bytes(ZIP + b"\0" * 20)
    assert reader.file_format(zipped) == reader.XLSX  # an .xlsx saved as .xls
    for name in ("old.xls", "OLD.XLS"):
        legacy = tmp_path / name
        legacy.write_bytes(OLE + b"\0" * 20)
        assert reader.file_format(legacy) == reader.XLS


def test_a_locked_xlsx_still_takes_the_xlsx_path(tmp_path):
    locked = tmp_path / "locked.xlsx"
    locked.write_bytes(OLE + b"\0" * 20)
    assert reader.file_format(locked) == reader.XLSX  # so it is reported as password-protected


def test_text_suffixes_are_case_insensitive(tmp_path):
    for name in ("a.CSV", "b.Txt", "c.TSV"):
        assert reader.file_format(tmp_path / name) == reader.DELIMITED


# --- .xls ------------------------------------------------------------------------------

def test_the_xls_sample_reads_like_a_workbook():
    path = sample("*.xls")
    assert reader.sheet_names(path) == ["actclt"]
    [grid] = reader.read_workbook(path)
    assert grid.source_format == "xls"
    assert grid.rows[0][:2] == ["Client", "Client Name"]
    # Whole numbers arrive as ints, as from an .xlsx.
    assert grid.rows[1][0] == 1047 and grid.rows[1][1] == "BankNewport"
    assert grid.styles[0][0]["emphasised"] is True  # the bold header row
    assert grid.height > 500 and grid.width == 32


def test_the_xls_sample_cleans_to_a_table():
    [grid] = reader.read_workbook(sample("*.xls"))
    [result] = extract_sheet(grid)
    assert len(result.frame) > 500 and len(result.frame.columns) == 32


def test_reading_one_xls_sheet_by_name():
    path = sample("*.xls")
    assert reader.read_sheet(path, "actclt").name == "actclt"
    with pytest.raises(KeyError):
        reader.read_sheet(path, "missing")


def test_a_broken_xls_is_classified_corrupt(tmp_path):
    broken = tmp_path / "broken.xls"
    broken.write_bytes(OLE + b"\0" * 512)
    with pytest.raises(Exception) as caught:
        reader.read_workbook(broken)
    assert failures.classify(broken, caught.value).kind == failures.CORRUPT


# --- .xlsb -----------------------------------------------------------------------------

def xlsb_sample() -> Path:
    """The smallest real .xlsb to hand: Sample_Formats first, then the August delivery."""
    found = sorted(SAMPLES.glob("*.xlsb")) + sorted(ROOT.parent.glob("Actual_*_Files/**/*Sigma*.xlsb"))
    if not found:
        pytest.skip("no .xlsb sample on disk")
    return min(found, key=lambda path: path.stat().st_size)


def test_an_xlsb_is_told_apart_from_an_xlsx_by_its_suffix(tmp_path):
    for name, expected in (("a.xlsb", reader.XLSB), ("b.XLSB", reader.XLSB), ("c.xlsx", reader.XLSX)):
        path = tmp_path / name
        path.write_bytes(ZIP + b"\0" * 20)
        assert reader.file_format(path) == expected


def test_the_xlsb_sample_reads_like_a_workbook():
    path = xlsb_sample()
    [name] = reader.sheet_names(path)
    [grid] = reader.read_workbook(path)
    assert grid.name == name and grid.source_format == "xlsb"
    assert grid.rows[0][1] == "RegionName" and grid.width == 69 and grid.height > 10
    assert reader.read_sheet(path, name).rows == grid.rows
    with pytest.raises(KeyError):
        reader.read_sheet(path, "missing")


def test_the_xlsb_sample_cleans_to_a_table():
    [grid] = reader.read_workbook(xlsb_sample())
    results = extract_sheet(grid)
    assert results and len(results[0].frame) > 10


def test_a_broken_xlsb_is_classified_corrupt(tmp_path):
    broken = tmp_path / "broken.xlsb"
    broken.write_bytes(ZIP + b"\0" * 512)
    with pytest.raises(Exception) as caught:
        reader.read_workbook(broken)
    assert failures.classify(broken, caught.value).kind == failures.CORRUPT


# --- .txt ------------------------------------------------------------------------------

def test_a_pipe_delimited_txt_reads_as_one_sheet():
    [grid] = reader.read_workbook(sample("*.txt"))
    assert grid.width > 50 and "pipe" in grid.source_format
    assert grid.rows[0][1] == "RegionName"


def test_a_tab_delimited_txt_is_split_on_tabs(tmp_path):
    path = tmp_path / "export.TXT"
    path.write_text("Policy\tPremium\nP-1\t100\nP-2\t250\n", encoding="utf-8")
    [grid] = reader.read_workbook(path)
    assert grid.rows[0] == ["Policy", "Premium"] and "tab" in grid.source_format


# --- through the API ---------------------------------------------------------------------

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


def _upload(client, path: Path):
    return client.post("/api/workbooks", files={"file": (path.name, path.read_bytes())})


def test_uploading_an_xls_lists_its_sheets_and_previews_them(client):
    response = _upload(client, sample("*.xls"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "xls" and [s["name"] for s in body["sheets"]] == ["actclt"]
    preview = client.get(f"/api/workbooks/{body['id']}/preview", params={"sheet": "actclt", "limit": 5})
    assert preview.status_code == 200, preview.text
    data = preview.json()
    # The preview shows the file as it is, so a padded cell keeps its padding.
    assert data["source_format"] == "xls" and data["rows"][0][0].strip() == "Client"


def test_uploading_a_txt_is_a_delimited_file(client):
    response = _upload(client, sample("*.txt"))
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "delimited"


def test_an_unknown_type_is_refused_with_the_new_list(client):
    response = client.post("/api/workbooks", files={"file": ("notes.docx", b"abc")})
    assert response.status_code == 415
    assert ".xls" in response.json()["error"]["advice"]


def _clean(client, path: Path, sheets=None):
    import time

    body = _upload(client, path).json()
    chosen = sheets or [sheet["name"] for sheet in body["sheets"]]
    response = client.post("/api/jobs", json={"workbook_id": body["id"], "sheets": chosen, "append": False})
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    for _ in range(1200):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] not in ("queued", "running"):
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_cleaning_job_runs_end_to_end_for_xls(client):
    status = _clean(client, sample("*.xls"))
    assert status["status"] == "succeeded", status
    assert status["rows_kept"] > 0


def test_uploading_an_xlsb_lists_its_sheet_and_previews_it(client):
    path = xlsb_sample()
    response = _upload(client, path)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "xlsb" and len(body["sheets"]) == 1
    sheet = body["sheets"][0]["name"]
    preview = client.get(f"/api/workbooks/{body['id']}/preview", params={"sheet": sheet, "limit": 3})
    assert preview.status_code == 200, preview.text
    assert preview.json()["source_format"] == "xlsb"


def test_cleaning_job_runs_end_to_end_for_xlsb(client):
    status = _clean(client, xlsb_sample())
    assert status["status"] == "succeeded", status
    assert status["rows_kept"] > 0

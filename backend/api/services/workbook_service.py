"""Accept an upload, check it is something the cleaner can open, and list its sheets.

Only sheet *names* are reported before cleaning. A dirty sheet's raw extent -- banners,
subtotals, blank rows, stray formatting -- says nothing reliable about the table inside
it, so row and column counts are left to the cleaner and shown after the run.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from fastapi import UploadFile
from fastapi.concurrency import run_in_threadpool

from ahi_clean import failures, reader

from .. import config
from ..errors import ApiError, conflict
from ..store import ACTIVE, Upload, store
from . import sheet_pool

_CHUNK = 1024 * 1024
# Rows scanned to decide whether a sheet holds anything at all.
_CONTENT_PROBE_ROWS = 2000


def _safe_name(filename: str) -> str:
    name = Path(filename or "").name.strip()
    cleaned = "".join(ch for ch in name if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    return cleaned or "workbook.xlsx"


def validate_filename(filename: str | None) -> str:
    if not filename:
        raise ApiError(400, "missing_file", "No file was received.", "Choose an Excel workbook to upload.")
    name = _safe_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix in config.ALLOWED_SUFFIXES:
        return name
    # Reuse the cleaner's own wording for formats it knows it cannot open.
    failure = failures.classify(Path(name), RuntimeError("unsupported"))
    if failure.kind == failures.UNSUPPORTED_FORMAT:
        raise ApiError(415, failure.kind, f"{name} can't be cleaned in this format.",
                       failure.advice, failure.detail)
    raise ApiError(
        415,
        failures.UNSUPPORTED_FORMAT,
        f"{name} is not a supported file type.",
        "Upload an .xlsx workbook, or a .csv / .tsv file.",
    )


async def save_upload(file: UploadFile) -> Upload:
    name = validate_filename(file.filename)
    upload_id = store.new_id()
    folder = config.UPLOAD_DIR / upload_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name

    size = 0
    try:
        with open(path, "wb") as handle:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise ApiError(
                        413,
                        "too_large",
                        f"{name} is larger than {config.MAX_UPLOAD_MB} MB.",
                        "Split the workbook, or remove sheets you don't need, and try again.",
                    )
                handle.write(chunk)
        if size == 0:
            raise ApiError(400, "empty_file", f"{name} is empty.", "Check the file and upload it again.")
        # openpyxl is blocking; keep the event loop free for progress polling.
        sheets = await run_in_threadpool(inspect, path)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    kind = "delimited" if path.suffix.lower() in config.DELIMITED_SUFFIXES else "workbook"
    upload = Upload(upload_id, name, path, size, kind, sheets)
    store.add_upload(upload)
    # A run usually follows an upload: start the cleaning workers now, off the clock --
    # only for a file big enough to be cleaned by them.
    if size >= config.PARALLEL_MIN_BYTES:
        sheet_pool.warm()
    return upload


def inspect(path: Path) -> list[dict]:
    """List the sheets, classifying an unreadable file the way the cleaner would."""
    if path.suffix.lower() in config.DELIMITED_SUFFIXES:
        # The delimited reader names its single sheet after the file.
        return [{"name": path.stem, "hidden": False, "has_content": True}]

    if not zipfile.is_zipfile(path):
        failure = failures.classify(path, zipfile.BadZipFile("File is not a zip file"))
        raise _unreadable(path.name, failure)
    try:
        # Opens the structure only: listing sheets must not parse every sheet's cells.
        with reader.open_workbook(path) as workbook:
            sheets = [
                {
                    "name": worksheet.title,
                    "hidden": getattr(worksheet, "sheet_state", "visible") != "visible",
                    "has_content": _has_content(worksheet),
                }
                for worksheet in workbook.worksheets
            ]
    except ApiError:
        raise
    except Exception as error:  # noqa: BLE001 - classified below
        raise _unreadable(path.name, failures.classify(path, error)) from error
    if not sheets:
        raise ApiError(422, failures.NO_TABLE, f"{path.name} has no worksheets.",
                       "Upload a workbook that contains at least one sheet.")
    return sheets


def _has_content(worksheet) -> bool:
    try:
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            if any(value not in (None, "") for value in row):
                return True
            if index >= _CONTENT_PROBE_ROWS:
                return False
    except Exception:  # noqa: BLE001 - a probe failure must not block the upload
        return True
    return False


def _unreadable(name: str, failure: failures.Failure) -> ApiError:
    headline = {
        failures.ENCRYPTED: f"{name} is password-protected.",
        failures.CORRUPT: f"{name} could not be opened.",
        failures.UNSUPPORTED_FORMAT: f"{name} is not in a supported format.",
    }.get(failure.kind, f"{name} could not be read.")
    return ApiError(422, failure.kind, headline, failure.advice, failure.detail)


def delete_upload(upload_id: str) -> None:
    upload = store.upload(upload_id)
    if any(job.status in ACTIVE for job in store.jobs_for(upload_id)):
        raise conflict(
            f"{upload.filename} is being cleaned right now.",
            "Wait for the run to finish, or cancel it, before removing the file.",
        )
    upload = store.remove_upload(upload_id)
    shutil.rmtree(upload.path.parent, ignore_errors=True)

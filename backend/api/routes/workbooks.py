from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from ..schemas import WorkbookOut
from ..services import workbook_service
from ..store import Upload, store

router = APIRouter(prefix="/api/workbooks", tags=["workbooks"])


def _out(upload: Upload) -> WorkbookOut:
    return WorkbookOut(
        id=upload.id,
        filename=upload.filename,
        size=upload.size,
        kind=upload.kind,
        sheets=upload.sheets,
        uploaded_at=upload.uploaded_at,
    )


@router.post("", response_model=WorkbookOut, status_code=201)
async def upload_workbook(file: UploadFile = File(...)) -> WorkbookOut:
    return _out(await workbook_service.save_upload(file))


@router.get("/{workbook_id}", response_model=WorkbookOut)
def get_workbook(workbook_id: str) -> WorkbookOut:
    return _out(store.upload(workbook_id))


@router.delete("/{workbook_id}", status_code=204)
def delete_workbook(workbook_id: str) -> None:
    workbook_service.delete_upload(workbook_id)


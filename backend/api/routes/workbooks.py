from __future__ import annotations

from fastapi import APIRouter, File, Query, UploadFile

from .. import config
from ..schemas import SourcePreview, WorkbookOut
from ..services import source_preview, workbook_service
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


@router.get("/{workbook_id}/preview", response_model=SourcePreview)
def preview_sheet(
    workbook_id: str,
    sheet: str = Query(..., min_length=1, max_length=255),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=config.PREVIEW_MAX_LIMIT),
    col_offset: int = Query(0, ge=0),
    col_limit: int = Query(20, ge=1, le=config.SOURCE_PREVIEW_MAX_COLUMNS),
) -> SourcePreview:
    """One sheet of the uploaded file as it is, before cleaning."""
    upload = store.upload(workbook_id)
    return SourcePreview(**source_preview.preview(upload, sheet, offset, limit, col_offset, col_limit))


@router.delete("/{workbook_id}", status_code=204)
def delete_workbook(workbook_id: str) -> None:
    workbook_service.delete_upload(workbook_id)


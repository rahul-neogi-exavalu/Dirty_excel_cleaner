from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from .. import progress
from ..auth import User, require_user
from ..schemas import ValidationCreate, ValidationFileUpdate, ValidationOutputUpdate, ValidationStage
from ..services import validation_service

router = APIRouter(prefix="/api/validations", tags=["validation"])


@router.post("", status_code=202)
async def create(request: ValidationCreate) -> dict:
    """Start validating: the files are checked here, the work runs on in the background.
    Follow it on ``/events``; the answer is the session as it starts (``running``)."""
    session = await run_in_threadpool(validation_service.start, request.job_ids, request.batch_id)
    return validation_service.session_view(session)


@router.get("/{session_id}")
def get(session_id: str) -> dict:
    """The review once it is built; until then how far it has got, or why it stopped."""
    return validation_service.session_view(validation_service.get(session_id))


@router.get("/{session_id}/events")
def events(session_id: str) -> StreamingResponse:
    """Server-Sent Events: ``progress`` while the session is built, then ``done`` with the
    review, or ``failed`` with the error. A stream opened later starts from the current state."""
    session = validation_service.get(session_id)
    return StreamingResponse(progress.stream(session.tracker), media_type="text/event-stream",
                             headers=progress.SSE_HEADERS)


@router.patch("/{session_id}/files/{job_id}")
async def update_file(session_id: str, job_id: str, request: ValidationFileUpdate) -> dict:
    session = await run_in_threadpool(
        validation_service.update_file, session_id, job_id, request.model_fields_set,
        request.pc_id, request.file_received_date, request.division_name,
    )
    return validation_service.session_out(session)


@router.patch("/{session_id}/outputs/{key}")
async def update_output(session_id: str, key: str, request: ValidationOutputUpdate) -> dict:
    session = await run_in_threadpool(
        validation_service.update_output, session_id, key, request.model_fields_set, request.mapping,
        request.reporting_start_date, request.reporting_end_date, request.choice, request.use_control_dates,
    )
    return validation_service.session_out(session)


@router.post("/{session_id}/stage")
async def stage(session_id: str, request: ValidationStage, user: User = Depends(require_user)) -> dict:
    # Staged by whoever is signed in.
    session = await run_in_threadpool(validation_service.stage, session_id, request.keys, user.user_name)
    return validation_service.session_out(session)

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from .. import auth
from ..auth import User
from ..services import job_history

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
async def list_history(
    type: str | None = Query(None, max_length=20),
    status: str | None = Query(None, max_length=20),
    q: str | None = Query(None, max_length=255),
    user_id: str | None = Query(None, max_length=40),
    before: str | None = Query(None, max_length=40),
    limit: int = Query(30, ge=1, le=100),
    user: User = Depends(auth.require_user),
) -> dict:
    """Runs grouped by job_id, newest first. Non-admins only ever see their own."""
    return await run_in_threadpool(
        lambda: job_history.list_runs(user, job_type=type, status=status, q=q, user_id=user_id,
                                      before=before, limit=limit))


@router.get("/{job_id}")
async def history_detail(job_id: str, user: User = Depends(auth.require_user)) -> dict:
    return await run_in_threadpool(job_history.run_detail, user, job_id)

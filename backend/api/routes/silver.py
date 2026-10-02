from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import config
from ..auth import User, require_user
from ..services import silver_service
from ..services.silver_service import Run

router = APIRouter(prefix="/api/silver", tags=["silver"])


class RunCreate(BaseModel):
    # Empty is allowed: a run can be only the removal of loads replaced in Bronze.
    ingestion_ids: list[str] = []


class MappingEdit(BaseModel):
    table_name: str
    bronze_column: str
    silver_column: str | None = None
    ignored: bool = False
    # More Silver columns the bronze column also loads into (the whole list). Sent alone,
    # it leaves the main choice as it is.
    also: list[str] | None = None


class SavedMappingEdit(BaseModel):
    """One DRT column mapping row, found by profit center, source column and its
    current Silver column (a source column can have two rows), and its new target."""

    profit_center: str
    pc_column: str
    silver_column_name: str | None = None
    new_silver_column_name: str | None = None


class RunApprove(BaseModel):
    # Ignored: the signed-in user is the reviewer. Kept so older clients still validate.
    reviewed_by: str | None = None


def run_out(run: Run) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "progress": round(run.progress, 4),
        "message": run.message,
        "error": run.error,
        "reviewed_by": run.reviewed_by,
        "notes": run.notes,
        "cleanup": run.cleanup,
        "lotl_rows": run.lotl.rows,
        "blockers": silver_service.problems(run),
        "result": run.result,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "tables": [
            {
                "table_name": review.table_name,
                "source_system": review.source_system,
                "pc_id": review.pc_id,
                "loads": [vars(load) for load in review.loads],
                "quality": review.quality,
                "mapping": [
                    {
                        "bronze_column": s.bronze_column,
                        "silver_column": s.silver_column,
                        "also": s.also,
                        "ignored": s.ignored,
                        "selection": s.selection,
                        # Methods that voted for the current choice.
                        "methods": [vote.method for vote in s.backers()],
                        "split": s.split,
                        "reason": s.reason,
                        "samples": s.samples,
                        "candidates": [
                            {
                                "silver_column": c.silver_column,
                                "recommended": c.recommended,
                                "support": c.support,
                                "votes": [
                                    {"method": v.method, "score": v.score, "reason": v.reason,
                                     "second_choice": v.second_choice}
                                    for v in c.votes
                                ],
                            }
                            for c in s.candidates
                        ],
                    }
                    for s in review.suggestions
                ],
            }
            for review in run.tables
        ],
    }


@router.get("/catalog")
def silver_catalog() -> dict:
    return {
        "file": config.SILVER_COLUMNS_FILE.name,
        # Every silver_detail column; role "mapped" ones are what a bronze column can map to.
        "columns": [vars(column) for column in silver_service.catalog()],
        "aggregate_columns": [column.name for column in silver_service.aggregate_catalog()],
        "semantic": bool(config.WORD2VEC_PATH),
        "ai": config.AI_ENABLED,
        # Provider and model only, never a key.
        "ai_label": config.AI_LABEL if config.AI_ENABLED else "",
    }


@router.get("/eligible")
async def eligible() -> dict:
    return await run_in_threadpool(silver_service.eligible)


@router.post("/runs", status_code=201)
async def create_run(request: RunCreate) -> dict:
    return run_out(await run_in_threadpool(silver_service.create_run, request.ingestion_ids))


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return run_out(silver_service.get_run(run_id))


@router.patch("/runs/{run_id}/mapping")
async def edit_run_mapping(run_id: str, request: MappingEdit) -> dict:
    run = await run_in_threadpool(
        silver_service.update_mapping, run_id, request.table_name, request.bronze_column,
        request.silver_column, request.ignored, request.model_fields_set, request.also,
    )
    return run_out(run)


class IgnoreUnmapped(BaseModel):
    table_name: str | None = None


@router.post("/runs/{run_id}/ignore-unmapped")
async def ignore_unmapped(run_id: str, request: IgnoreUnmapped) -> dict:
    run = await run_in_threadpool(silver_service.ignore_unmapped, run_id, request.table_name)
    return run_out(run)


@router.post("/runs/{run_id}/approve", status_code=202)
async def approve(run_id: str, request: RunApprove, user: User = Depends(require_user)) -> dict:
    return run_out(await run_in_threadpool(silver_service.approve, run_id, user.user_name, user.user_id))


@router.get("/mapping")
async def saved_mapping() -> list[dict]:
    return await run_in_threadpool(silver_service.saved_mapping)


@router.patch("/mapping")
async def edit_saved_mapping(request: SavedMappingEdit) -> dict:
    return await run_in_threadpool(
        silver_service.edit_saved_mapping, request.profit_center, request.pc_column,
        request.silver_column_name, request.new_silver_column_name,
    )


@router.get("/aggregate")
async def aggregate() -> list[dict]:
    return await run_in_threadpool(silver_service.aggregate)

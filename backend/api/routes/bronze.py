from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from .. import db
from ..auth import User, require_user
from ..schemas import PlanApprove, PlanCreate, PlanFile, PlanItemUpdate, PlanOut
from ..services import bronze_service, validation_service
from ..services.bronze_service import Plan

router = APIRouter(prefix="/api/bronze", tags=["bronze"])


def plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        id=plan.id,
        batch_id=plan.batch_id,
        status=plan.status,
        progress=round(plan.progress, 4),
        message=plan.message,
        error=plan.error,
        reviewed_by=plan.reviewed_by,
        files=[
            PlanFile(
                key=file.key,
                control_id=file.control_id,
                file_name=file.file_name,
                output_name=file.output_name,
                source_system=file.source_system,
                pc_id=file.pc_id,
                division_name=file.division_name,
                file_received_date=file.file_received_date.isoformat() if file.file_received_date else None,
                reporting_start_date=file.reporting_start_date.isoformat(),
                reporting_end_date=file.reporting_end_date.isoformat(),
                reporting_period_type=file.reporting_period_type,
                date_detail=file.date_detail,
                processing_action=file.processing_action,
                file_replaced=file.file_replaced,
                replace_month=file.replace_month,
                rows=file.rows,
                fitness=file.fitness,
                staging_table=file.staging_table,
                staged_at=file.staged_at,
            )
            for file in plan.files
        ],
        items=[item.as_dict() for item in plan.items],
        blockers=bronze_service.problems(plan),
        results=plan.results,
        created_at=plan.created_at,
        started_at=plan.started_at,
        finished_at=plan.finished_at,
    )


@router.get("/status")
async def status() -> dict:
    return await run_in_threadpool(db.status)


@router.get("/tables")
async def tables() -> list[dict]:
    return await run_in_threadpool(bronze_service.list_tables)


@router.get("/control")
async def control(
    source_system: str | None = Query(None, max_length=64),
    status: str | None = Query(None, pattern="^(pending|loaded|rejected|listed)$"),
    limit: int = Query(500, ge=1, le=5000),
) -> list[dict]:
    """The control table, newest first."""
    return await run_in_threadpool(validation_service.control_rows, source_system, status, limit)


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(request: PlanCreate) -> PlanOut:
    plan = await run_in_threadpool(bronze_service.create_plan, request.control_ids, request.batch_id)
    return plan_out(plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str) -> PlanOut:
    return plan_out(bronze_service.current_plan(plan_id))


@router.patch("/plans/{plan_id}/items/{key}", response_model=PlanOut)
async def update_item(plan_id: str, key: str, request: PlanItemUpdate) -> PlanOut:
    plan = await run_in_threadpool(
        bronze_service.update_item, plan_id, key, request.table_name, request.action, request.model_fields_set,
    )
    return plan_out(plan)


@router.post("/plans/{plan_id}/approve", response_model=PlanOut, status_code=202)
async def approve(plan_id: str, request: PlanApprove, user: User = Depends(require_user)) -> PlanOut:
    # The reviewer is whoever is signed in, never a name typed into the request.
    plan = await run_in_threadpool(bronze_service.approve, plan_id, user.user_name, request.confirmed, user.user_id)
    return plan_out(plan)

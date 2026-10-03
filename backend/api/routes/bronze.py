from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from .. import db
from ..auth import User, require_user
from ..schemas import PlanApprove, PlanCreate, PlanFile, PlanFileUpdate, PlanItemUpdate, PlanOut
from ..services import bronze_service
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
                job_id=file.job_id,
                file_name=file.file_name,
                source_system=file.source_system,
                detected_source_system=file.detected_source_system,
                period_start=file.period.start if file.period else None,
                period_end=file.period.end if file.period else None,
                detected_period_start=file.detected_period.start if file.detected_period else None,
                detected_period_end=file.detected_period.end if file.detected_period else None,
                period_source=file.period_source,
                period_candidates=[
                    {k: v for k, v in candidate.items() if k != "rank"} for candidate in file.period_candidates
                ],
                pc_id=file.pc_id,
                detected_pc_id=file.detected_pc_id,
                file_date=file.file_date.isoformat() if file.file_date else None,
                detected_file_date=file.detected_file_date.isoformat() if file.detected_file_date else None,
                division_name=file.division_name,
                division_matches=file.division_matches,
                division_options=bronze_service.division_options(file, plan.divisions),
                warnings=file.warnings,
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


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(request: PlanCreate) -> PlanOut:
    plan = await run_in_threadpool(bronze_service.create_plan, request.job_ids, request.batch_id)
    return plan_out(plan)


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str) -> PlanOut:
    return plan_out(bronze_service.get_plan(plan_id))


@router.patch("/plans/{plan_id}/files/{job_id}", response_model=PlanOut)
async def update_file(plan_id: str, job_id: str, request: PlanFileUpdate) -> PlanOut:
    plan = await run_in_threadpool(
        bronze_service.update_file, plan_id, job_id, request.source_system,
        request.period_start, request.period_end, request.model_fields_set,
        request.pc_id, request.file_date, request.division_name,
    )
    return plan_out(plan)


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

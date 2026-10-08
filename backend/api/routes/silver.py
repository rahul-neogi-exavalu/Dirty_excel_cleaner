from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ahi_silver import aggregate as aggregates
from ahi_silver import matching
from ahi_silver.catalog import targets as drt_targets

from .. import config
from ..auth import User, require_user
from ..services import silver_service
from ..services.silver_service import Run

router = APIRouter(prefix="/api/silver", tags=["silver"])


class TargetEdit(BaseModel):
    """From the Silver side: which bronze column loads this DRT column (None: none does)."""

    table_name: str
    silver_column: str
    bronze_column: str | None = None


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


class SavedMappingRow(BaseModel):
    """One DRT column mapping row, found by all four of its values (a source column can
    have two rows; the four together name exactly one)."""

    profit_center: str
    pc_column: str
    drt_column: str | None = None
    silver_column_name: str | None = None


class SavedMappingEdit(SavedMappingRow):
    """The row, and the Silver column it should load into instead."""

    new_silver_column_name: str | None = None


class SpreadEdit(BaseModel):
    """An aggregated table's columns that are one measure across a dimension (their
    headers its values): the measure, and the aggregate column the headers fill (None:
    the next aggregation_dimension slot, named ``label``). ``rollups``: columns totalling
    others, left out (sent alone, kept as they are)."""

    table_name: str
    columns: list[str] = []
    measure: str | None = None
    dimension: str | None = None
    label: str | None = None
    rollups: list[str] | None = None


class JoinKey(BaseModel):
    left: str
    right: str


class JoinEdit(BaseModel):
    """Join two tables of files that came together: ``how`` left, right or inner (None:
    undo the join), on ``keys`` (left column = right column; several make one key)."""

    left_table: str
    right_table: str
    how: str | None = None
    keys: list[JoinKey] = []
    ignore_case: bool = True


class RunApprove(BaseModel):
    # Ignored: the signed-in user is the reviewer. Kept so older clients still validate.
    reviewed_by: str | None = None


def run_out(run: Run) -> dict:
    targets = drt_targets(silver_service.catalog())
    aggregate_targets = aggregates.targets(silver_service.aggregate_catalog())
    return {
        "id": run.id,
        "status": run.status,
        "progress": round(run.progress, 4),
        "message": run.message,
        "error": run.error,
        "reviewed_by": run.reviewed_by,
        "notes": run.notes,
        "cleanup": run.cleanup,
        "lotl_rows": run.lotl_rows,
        "blockers": silver_service.problems(run),
        "result": run.result,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        # What an aggregated table's columns can be mapped onto, measures apart.
        "aggregate_targets": [{"name": column.name, "measure": aggregates.is_measure(column)}
                              for column in aggregate_targets],
        # Tables of files that came together, which the reviewer joins: the suggested keys.
        "links": [{key: value for key, value in link.items() if key != "saved"} | {"saved": bool(link.get("saved"))}
                  for link in run.links],
        "tables": [
            {
                "table_name": review.table_name,
                "source_system": review.source_system,
                "pc_id": review.pc_id,
                "pc_ids": review.pc_ids,
                "loads": [vars(load) for load in review.loads],
                "quality": review.quality,
                # The profit center's own aggregates: to silver_aggregate, with the columns
                # spread across a dimension and the roll-ups left out.
                "aggregated": review.aggregated,
                "spread": review.spread,
                "rollups": review.rollups,
                # The cleaner's sheet column: lineage, never mapped.
                "lineage": review.lineage,
                # A join with the table of a file that came with this one, and its effect;
                # or the table this one is joined into (it is not loaded on its own).
                "join": review.join,
                "joined_into": review.joined_into,
                "columns": silver_service._columns(review),
                "mapping": [
                    {
                        "bronze_column": s.bronze_column,
                        # The header as the source file wrote it (None for loads made
                        # before headers were kept).
                        "source_header": review.headers.get(s.bronze_column),
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
                # The same review from the Silver side: every DRT column, the bronze column
                # that loads it (or none), and the bronze columns voted for it, best first.
                "targets": [
                    {
                        "silver_column": pick.silver_column,
                        "bronze_column": pick.bronze_column,
                        "selection": pick.selection,
                        "votes": [_vote(v) for v in pick.votes],
                        "candidates": [
                            {"bronze_column": bronze, "recommended": c.recommended, "support": c.support,
                             "votes": [_vote(v) for v in c.counted]}
                            for bronze, c in pick.candidates
                        ],
                    }
                    for pick in matching.by_silver(review.suggestions,
                                                   aggregate_targets if review.aggregated else targets)
                ],
            }
            for review in run.tables
        ],
    }


def _vote(vote) -> dict:
    return {"method": vote.method, "score": vote.score, "reason": vote.reason, "second_choice": vote.second_choice}


@router.get("/catalog")
def silver_catalog() -> dict:
    return {
        "file": config.SILVER_COLUMNS_FILE.name,
        # Every silver_transaction column. ``target``: a DRT column, which a bronze column can
        # map to; the dropdowns offer only these.
        "columns": [{**vars(column), "target": column.is_target} for column in silver_service.catalog()],
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
async def get_run(run_id: str) -> dict:
    return run_out(await run_in_threadpool(silver_service.get_run, run_id))


@router.patch("/runs/{run_id}/mapping")
async def edit_run_mapping(run_id: str, request: MappingEdit) -> dict:
    run = await run_in_threadpool(
        silver_service.update_mapping, run_id, request.table_name, request.bronze_column,
        request.silver_column, request.ignored, request.model_fields_set, request.also,
    )
    return run_out(run)


@router.patch("/runs/{run_id}/targets")
async def assign_target(run_id: str, request: TargetEdit) -> dict:
    """From the Silver side: which bronze column loads a DRT column, or none."""
    run = await run_in_threadpool(
        silver_service.assign_target, run_id, request.table_name, request.silver_column, request.bronze_column)
    return run_out(run)


class IgnoreUnmapped(BaseModel):
    table_name: str | None = None


@router.patch("/runs/{run_id}/join")
async def edit_join(run_id: str, request: JoinEdit) -> dict:
    """Join two tables of files that came together, or undo the join."""
    run = await run_in_threadpool(
        silver_service.update_join, run_id, request.left_table, request.right_table, request.how,
        [(key.left, key.right) for key in request.keys], request.ignore_case)
    return run_out(run)


@router.patch("/runs/{run_id}/spread")
async def edit_spread(run_id: str, request: SpreadEdit) -> dict:
    """An aggregated table's measure spread across columns, and its roll-up columns."""
    run = await run_in_threadpool(
        silver_service.update_spread, run_id, request.table_name, request.columns, request.measure,
        request.dimension, request.label, request.rollups)
    return run_out(run)


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
        silver_service.edit_saved_mapping, request.profit_center, request.pc_column, request.drt_column,
        request.silver_column_name, request.new_silver_column_name,
    )


@router.delete("/mapping", status_code=204, response_class=Response)
async def delete_saved_mapping(request: SavedMappingRow) -> None:
    await run_in_threadpool(
        silver_service.delete_saved_mapping, request.profit_center, request.pc_column, request.drt_column,
        request.silver_column_name,
    )


@router.get("/aggregate")
async def aggregate() -> list[dict]:
    return await run_in_threadpool(silver_service.aggregate)

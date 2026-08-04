"""Admin ingestion run list/detail endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from mc_contracts.admin_modules import IngestionRunDetail, IngestionRunListResponse
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.repositories.ingestion_run_repository import (
    DEFAULT_INGESTION_RUN_SORT_BY,
    DEFAULT_INGESTION_RUN_SORT_DIR,
    INGESTION_RUN_SORT_DIRS,
    INGESTION_RUN_SORT_KEYS,
    IngestionRunRepository,
)
from platform_service.deps import get_db
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter

router = APIRouter(prefix="/admin", tags=["admin-ingestion-runs"])


@router.get("/ingestion-runs", response_model=IngestionRunListResponse)
async def list_ingestion_runs(
    status: str | None = Query(None),
    q: str | None = Query(
        None,
        description="Case-insensitive substring match on original_filename or title",
    ),
    sort_by: str = Query(
        DEFAULT_INGESTION_RUN_SORT_BY,
        description="started_at | completed_at | status | document_label",
    ),
    sort_dir: str = Query(
        DEFAULT_INGESTION_RUN_SORT_DIR,
        description="asc | desc",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> IngestionRunListResponse:
    if sort_by not in INGESTION_RUN_SORT_KEYS:
        raise AppError(
            ErrorCode.INVALID_QUERY.value,
            f"sort_by must be one of: {', '.join(sorted(INGESTION_RUN_SORT_KEYS))}",
            status=422,
        )
    if sort_dir not in INGESTION_RUN_SORT_DIRS:
        raise AppError(
            ErrorCode.INVALID_QUERY.value,
            f"sort_dir must be one of: {', '.join(sorted(INGESTION_RUN_SORT_DIRS))}",
            status=422,
        )
    filename_query = q.strip() if q and q.strip() else None
    repo = IngestionRunRepository(session)
    total_runs = await repo.count_ingestion_runs(status=status, filename_query=filename_query)
    rows = await repo.list_ingestion_runs(
        status=status,
        filename_query=filename_query,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    total_pages = (total_runs + limit - 1) // limit if total_runs > 0 else 0
    return IngestionRunListResponse(
        runs=await IngestionRunPresenter(session).present_summaries(rows),
        total_runs=total_runs,
        total_pages=total_pages,
        limit=limit,
        offset=offset,
    )


@router.get("/ingestion-runs/{run_id}", response_model=IngestionRunDetail)
async def get_ingestion_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> IngestionRunDetail:
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise AppError(ErrorCode.RUN_NOT_FOUND.value, "ingestion run not found", status=404)
    return await IngestionRunPresenter(session).present_detail(run)

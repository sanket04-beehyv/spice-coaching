"""Admin ingestion run list/detail endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from mc_contracts.admin_modules import IngestionRunDetail, IngestionRunListResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.deps import get_db
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter

router = APIRouter(prefix="/admin", tags=["admin-ingestion-runs"])


@router.get("/ingestion-runs", response_model=IngestionRunListResponse)
async def list_ingestion_runs(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> IngestionRunListResponse:
    count_stmt = select(func.count()).select_from(IngestionRun)
    list_stmt = select(IngestionRun)
    if status:
        count_stmt = count_stmt.where(IngestionRun.status == status)
        list_stmt = list_stmt.where(IngestionRun.status == status)
    total_runs = int(await session.scalar(count_stmt) or 0)
    list_stmt = list_stmt.order_by(IngestionRun.started_at.desc()).limit(limit).offset(offset)
    rows = list((await session.execute(list_stmt)).scalars().all())
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
        raise HTTPException(status_code=404, detail="ingestion run not found")
    return await IngestionRunPresenter(session).present_detail(run)

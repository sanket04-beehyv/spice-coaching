"""Admin ingestion run list/detail endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from mc_contracts.admin_modules import IngestionRunDetail, IngestionRunSummary
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.deps import get_db
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter

router = APIRouter(prefix="/admin", tags=["admin-ingestion-runs"])


@router.get("/ingestion-runs", response_model=list[IngestionRunSummary])
async def list_ingestion_runs(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> list[IngestionRunSummary]:
    stmt = select(IngestionRun)
    if status:
        stmt = stmt.where(IngestionRun.status == status)
    stmt = stmt.order_by(IngestionRun.started_at.desc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    return [IngestionRunPresenter.present_summary(r) for r in rows]


@router.get("/ingestion-runs/{run_id}", response_model=IngestionRunDetail)
async def get_ingestion_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> IngestionRunDetail:
    run = await session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ingestion run not found")
    return await IngestionRunPresenter(session).present_detail(run)

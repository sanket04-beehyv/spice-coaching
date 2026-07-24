"""Admin module lifecycle and performance analytics."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from mc_contracts.admin_module_analytics import (
    ModuleLifecycleActionRequest,
    ModuleLifecycleEventPayload,
    ModuleLifecycleStatePayload,
    ModulePerformanceSummary,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.module_availability import VALID_LIFECYCLE_STATUSES
from platform_service.db.repositories.module_analytics_repository import ModuleAnalyticsRepository
from platform_service.db.repositories.module_lifecycle_repository import (
    ModuleLifecycleError,
    ModuleLifecycleRepository,
    ModuleLifecycleState,
    ModuleNotFoundError,
)
from platform_service.deps import get_db
from platform_service.services.attribution_audit import record_attribution_event

router = APIRouter(prefix="/admin", tags=["admin-module-analytics"])
logger = logging.getLogger(__name__)


def _validate_lifecycle_status(lifecycle_status: str | None) -> str | None:
    if lifecycle_status is None:
        return None
    if lifecycle_status not in VALID_LIFECYCLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"lifecycle_status must be one of: {', '.join(sorted(VALID_LIFECYCLE_STATUSES))}",
        )
    return lifecycle_status


def _lifecycle_state_payload(state: ModuleLifecycleState) -> ModuleLifecycleStatePayload:
    return ModuleLifecycleStatePayload(
        module_id=state.module_id,
        module_family_id=state.module_family_id,
        lifecycle_status=state.lifecycle_status,
        first_activated_at=state.first_activated_at,
        last_deactivated_at=state.last_deactivated_at,
        last_reactivated_at=state.last_reactivated_at,
    )


@router.post("/modules/{module_id}/deactivate", response_model=ModuleLifecycleStatePayload)
async def deactivate_module(
    module_id: UUID,
    body: ModuleLifecycleActionRequest | None = None,
    session: AsyncSession = Depends(get_db),
) -> ModuleLifecycleStatePayload:
    repo = ModuleLifecycleRepository(session)
    action = body or ModuleLifecycleActionRequest()
    try:
        state = await repo.deactivate(module_id, actor_id=action.actor_id, reason=action.reason)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModuleLifecycleError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    logger.info(
        "module_deactivated module_id=%s family_id=%s actor_id=%s",
        module_id,
        state.module_family_id,
        action.actor_id,
    )
    await record_attribution_event(
        session,
        event_type="module_deactivated",
        actor=str(action.actor_id) if action.actor_id else "admin",
        module_id=module_id,
        payload={"module_family_id": str(state.module_family_id), "reason": action.reason},
    )
    await session.commit()
    return _lifecycle_state_payload(state)


@router.post("/modules/{module_id}/reactivate", response_model=ModuleLifecycleStatePayload)
async def reactivate_module(
    module_id: UUID,
    body: ModuleLifecycleActionRequest | None = None,
    session: AsyncSession = Depends(get_db),
) -> ModuleLifecycleStatePayload:
    repo = ModuleLifecycleRepository(session)
    action = body or ModuleLifecycleActionRequest()
    try:
        state = await repo.reactivate(module_id, actor_id=action.actor_id, reason=action.reason)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModuleLifecycleError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    logger.info(
        "module_reactivated module_id=%s family_id=%s actor_id=%s",
        module_id,
        state.module_family_id,
        action.actor_id,
    )
    await record_attribution_event(
        session,
        event_type="module_reactivated",
        actor=str(action.actor_id) if action.actor_id else "admin",
        module_id=module_id,
        payload={"module_family_id": str(state.module_family_id), "reason": action.reason},
    )
    await session.commit()
    return _lifecycle_state_payload(state)


@router.get(
    "/modules/{module_id}/lifecycle",
    response_model=list[ModuleLifecycleEventPayload],
)
async def list_module_lifecycle(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> list[ModuleLifecycleEventPayload]:
    repo = ModuleLifecycleRepository(session)
    module = await session.get(Module, module_id)
    if module is None:
        raise HTTPException(status_code=404, detail=f"module {module_id} not found")
    events = await repo.list_lifecycle_events(module_id)
    return [
        ModuleLifecycleEventPayload(
            id=e.id,
            module_id=e.module_id,
            event_type=e.event_type,
            occurred_at=e.occurred_at,
            actor_id=e.actor_id,
            reason=e.reason,
        )
        for e in events
    ]


@router.get("/analytics/modules", response_model=list[ModulePerformanceSummary])
async def module_performance_analytics(
    from_dt: datetime | None = Query(None, alias="from"),
    to_dt: datetime | None = Query(None, alias="to"),
    lifecycle_status: str | None = Query(None, description="draft | published | retired | deactivated"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> list[ModulePerformanceSummary]:
    lifecycle_status = _validate_lifecycle_status(lifecycle_status)
    rows = await ModuleAnalyticsRepository(session).module_performance(
        from_dt=from_dt,
        to_dt=to_dt,
        lifecycle_status=lifecycle_status,
        limit=limit,
        offset=offset,
    )
    return [
        ModulePerformanceSummary(
            module_family_id=row.module_family_id,
            module_id=row.module_id,
            module_code=row.module_code,
            title_bn=row.title_bn,
            title_en=row.title_en,
            lifecycle_status=row.lifecycle_status,
            family_created_at=row.family_created_at,
            first_activated_at=row.first_activated_at,
            last_deactivated_at=row.last_deactivated_at,
            last_reactivated_at=row.last_reactivated_at,
            unique_chws_attempted=row.unique_chws_attempted,
            unique_chws_completed=row.unique_chws_completed,
            total_attempts_in_range=row.total_attempts_in_range,
        )
        for row in rows
    ]

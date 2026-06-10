"""Morning cards endpoint — CHW-facing morning module suggestions.

Canonical path:
  GET /morning/cards?chw_id={int} → MorningCardsResponse
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mc_contracts.morning import MorningCardsResponse
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import (
    resolve_chw_id_for_device_route,
    resolve_tenant_id_for_device_route,
)
from platform_service.deps import get_db
from platform_service.services.morning_suggestion_service import MorningSuggestionService

router = APIRouter(prefix="/morning", tags=["morning"])


@router.get("/cards", response_model=MorningCardsResponse)
async def get_morning_cards(
    request: Request,
    chw_id: int | None = Query(
        default=None,
        description="Optional CHW id (integer). When omitted, return recently added modules.",
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID. When omitted, only tenant-global content is eligible.",
    ),
    db: AsyncSession = Depends(get_db),
) -> MorningCardsResponse:
    effective_chw_id = resolve_chw_id_for_device_route(request, chw_id)
    effective_tenant = resolve_tenant_id_for_device_route(request, tenant_id) or UUID(int=0)
    return await MorningSuggestionService(db).get_morning_cards(
        chw_id=effective_chw_id,
        tenant_id=effective_tenant,
    )

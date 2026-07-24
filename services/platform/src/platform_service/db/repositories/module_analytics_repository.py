"""Admin module performance aggregates from PostgreSQL completion state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.module_availability import analytics_timestamp_in_range, is_training_module_family


@dataclass(frozen=True)
class ModulePerformanceRow:
    module_family_id: UUID
    module_id: UUID | None
    module_code: str
    title_bn: str | None
    title_en: str | None
    lifecycle_status: str
    family_created_at: datetime
    first_activated_at: datetime | None
    last_deactivated_at: datetime | None
    last_reactivated_at: datetime | None
    unique_chws_attempted: int
    unique_chws_completed: int
    total_attempts_in_range: int


def _locale_title(title_localized: Any, locale: str) -> str | None:
    if not isinstance(title_localized, dict):
        return None
    value = title_localized.get(locale)
    if isinstance(value, str) and value.strip():
        return value
    return None


class ModuleAnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def module_performance(
        self,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        lifecycle_status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ModulePerformanceRow]:
        attempt_in_range = analytics_timestamp_in_range(CHWModuleCompletion.latest_attempt_at, from_dt, to_dt)
        complete_in_range = analytics_timestamp_in_range(CHWModuleCompletion.completed_at, from_dt, to_dt)

        stmt = (
            select(
                ModuleFamily.id.label("module_family_id"),
                Module.id.label("module_id"),
                ModuleFamily.module_code,
                Module.lifecycle_status,
                ModuleFamily.created_at.label("family_created_at"),
                Module.first_activated_at,
                Module.last_deactivated_at,
                Module.last_reactivated_at,
                Module.title_localized,
                func.count(func.distinct(case((attempt_in_range, CHWModuleCompletion.chw_id)))).label(
                    "unique_chws_attempted"
                ),
                func.count(func.distinct(case((complete_in_range, CHWModuleCompletion.chw_id)))).label(
                    "unique_chws_completed"
                ),
                func.count(case((attempt_in_range, 1))).label("total_attempts_in_range"),
            )
            .select_from(ModuleFamily)
            .outerjoin(Module, Module.id == ModuleFamily.current_published_module_id)
            .outerjoin(CHWModuleCompletion, CHWModuleCompletion.module_family_id == ModuleFamily.id)
            .group_by(
                ModuleFamily.id,
                Module.id,
                ModuleFamily.module_code,
                Module.lifecycle_status,
                ModuleFamily.created_at,
                Module.first_activated_at,
                Module.last_deactivated_at,
                Module.last_reactivated_at,
                Module.title_localized,
            )
            .order_by(ModuleFamily.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        stmt = stmt.where(is_training_module_family())
        if lifecycle_status is not None:
            stmt = stmt.where(Module.lifecycle_status == lifecycle_status)

        rows = (await self._session.execute(stmt)).all()
        return [
            ModulePerformanceRow(
                module_family_id=row.module_family_id,
                module_id=row.module_id,
                module_code=row.module_code,
                title_bn=_locale_title(row.title_localized, "bn"),
                title_en=_locale_title(row.title_localized, "en"),
                lifecycle_status=row.lifecycle_status or "draft",
                family_created_at=row.family_created_at,
                first_activated_at=row.first_activated_at,
                last_deactivated_at=row.last_deactivated_at,
                last_reactivated_at=row.last_reactivated_at,
                unique_chws_attempted=int(row.unique_chws_attempted or 0),
                unique_chws_completed=int(row.unique_chws_completed or 0),
                total_attempts_in_range=int(row.total_attempts_in_range or 0),
            )
            for row in rows
        ]

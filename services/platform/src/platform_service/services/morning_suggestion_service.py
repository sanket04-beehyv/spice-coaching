from __future__ import annotations

from uuid import UUID

from mc_contracts.morning import MorningCardsResponse, MorningModuleSuggestionItem
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.learning_points_service import LearningPointsService
from platform_service.services.module_suggestion_service import ModuleSuggestionService


class MorningSuggestionService:
    """Morning module suggestions for a CHW.

    Thin wrapper over ModuleSuggestionService to provide an API-friendly
    response model.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._module_suggestions = ModuleSuggestionService(session)
        self._modules = ModuleRepository(session)

    async def get_morning_cards(
        self,
        *,
        chw_id: int | None,
        tenant_id: UUID,
    ) -> MorningCardsResponse:
        if chw_id is None:
            mods = await self._modules.list_recent_published_one_per_family(tenant_id=tenant_id, limit=5)
            return MorningCardsResponse(
                items=[
                    MorningModuleSuggestionItem(
                        module_id=m.id,
                        module_family_id=m.module_family_id,
                        source="fallback",
                        behavioural_gap_id=m.primary_gap_id,
                    )
                    for m in mods
                ],
                total_points=0,
            )

        items = await self._module_suggestions.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
        total_pts = await LearningPointsService(self._session).get_total_points(chw_id=chw_id)
        return MorningCardsResponse(
            items=[
                MorningModuleSuggestionItem(
                    module_id=i.module_id,
                    module_family_id=i.module_family_id,
                    source=i.source,
                    behavioural_gap_id=i.behavioural_gap_id,
                    quiz_id=i.quiz_id,
                )
                for i in items
            ],
            total_points=total_pts,
        )

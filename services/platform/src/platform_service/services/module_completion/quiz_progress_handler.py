"""Per-question quiz progress and module completion coverage."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.chw_module_quiz_progress import CHWModuleQuizProgress
from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_completion_repository import (
    ModuleCompletionRepository,
)

logger = logging.getLogger(__name__)


class QuizProgressHandler:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_question_attempted_and_maybe_complete(
        self,
        *,
        chw_id: int,
        tenant_uuid: UUID | None,
        module: Module,
        quiz_id: UUID,
    ) -> None:
        """Persist per-question attempt progress and mark module completed when coverage hits 100%."""
        if not await self._validate_quiz_belongs_to_module(module=module, quiz_id=quiz_id):
            return

        # Idempotent upsert: (chw_id, module_id, quiz_id) PK.
        stmt = (
            insert(CHWModuleQuizProgress)
            .values(
                chw_id=chw_id,
                module_id=module.id,
                quiz_id=quiz_id,
                tenant_id=tenant_uuid,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CHWModuleQuizProgress.chw_id,
                    CHWModuleQuizProgress.module_id,
                    CHWModuleQuizProgress.quiz_id,
                ]
            )
        )
        await self._session.execute(stmt)

        quiz_ids, covered_count = await self._quiz_coverage_count(chw_id=chw_id, module=module)
        if not quiz_ids:
            return

        if covered_count >= len(quiz_ids):
            repo = ModuleCompletionRepository(self._session)
            comp = await repo.get(chw_id=chw_id, module_family_id=module.module_family_id)
            if comp is None:
                self._session.add(
                    CHWModuleCompletion(
                        chw_id=chw_id,
                        module_family_id=module.module_family_id,
                        tenant_id=tenant_uuid,
                        attempts_since_last_pass=0,
                    )
                )
                await self._session.flush()
            await repo.mark_completed(
                chw_id=chw_id,
                module_family_id=module.module_family_id,
                completed_module_id=module.id,
            )

    async def _validate_quiz_belongs_to_module(
        self,
        *,
        module: Module,
        quiz_id: UUID,
    ) -> bool:
        quiz_row = await self._session.get(ModuleQuizQuestion, quiz_id)
        if quiz_row is None or quiz_row.module_id != module.id:
            logger.warning(
                "module_completion: quiz_id=%s not found for module_id=%s; skipping progress",
                quiz_id,
                module.id,
            )
            return False
        return True

    async def _quiz_coverage_count(
        self,
        *,
        chw_id: int,
        module: Module,
    ) -> tuple[list[UUID], int]:
        quiz_ids = list(
            (
                await self._session.execute(
                    select(ModuleQuizQuestion.id).where(ModuleQuizQuestion.module_id == module.id)
                )
            )
            .scalars()
            .all()
        )
        if not quiz_ids:
            return quiz_ids, 0

        covered_count = int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(CHWModuleQuizProgress)
                    .where(
                        CHWModuleQuizProgress.chw_id == chw_id,
                        CHWModuleQuizProgress.module_id == module.id,
                        CHWModuleQuizProgress.quiz_id.in_(quiz_ids),
                    )
                )
            ).scalar_one()
        )
        return quiz_ids, covered_count

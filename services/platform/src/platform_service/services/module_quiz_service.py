"""Persist quiz question versions when admin edits a module."""

from __future__ import annotations

import uuid
from uuid import UUID

from mc_contracts.admin_modules import QuizQuestionEditRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_quiz_question import ModuleQuizQuestion


class ModuleQuizService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_questions(
        self,
        module_id: UUID,
        quiz_data: list[QuizQuestionEditRequest | dict],
    ) -> None:
        """Write versioned quiz rows for a newly created module version."""
        for idx, q_item in enumerate(quiz_data, start=1):
            if isinstance(q_item, dict):
                q = QuizQuestionEditRequest(**q_item)
            else:
                q = q_item

            question_family_id = uuid.uuid4()
            question_version = 1

            if q.id is not None and q.id != "":
                try:
                    u_id = UUID(str(q.id))
                    stmt = select(ModuleQuizQuestion).where(ModuleQuizQuestion.id == u_id)
                    existing_q = (await self._session.execute(stmt)).scalar_one_or_none()
                    if existing_q:
                        question_family_id = existing_q.question_family_id
                        stmt_max = select(func.max(ModuleQuizQuestion.question_version)).where(
                            ModuleQuizQuestion.question_family_id == question_family_id
                        )
                        max_v = (await self._session.execute(stmt_max)).scalar_one() or 0
                        question_version = max_v + 1
                except ValueError:
                    pass

            row = ModuleQuizQuestion(
                module_id=module_id,
                question_order=q.question_order if q.question_order is not None else idx,
                question_family_id=question_family_id,
                question_version=question_version,
                case_setup_localized=q.case_setup,
                question_localized=q.question or {},
                question_type="single_select",
                options_localized=q.options,
                correct_indices=q.correct_indices,
                explanation_localized=q.explanation,
                difficulty=q.difficulty,
            )
            self._session.add(row)

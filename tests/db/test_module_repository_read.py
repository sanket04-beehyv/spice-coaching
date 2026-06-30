"""ModuleRepository — get_module and list_quiz_questions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_repository import (
    ModuleRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.db.conftest import (
    _make_family,
    _make_module,
)

pytestmark = [requires_db, pytest.mark.asyncio]


class TestGetModule:
    async def test_returns_none_for_missing(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        assert await repo.get_module(uuid4()) is None

    async def test_returns_existing_row(self, db_session: AsyncSession) -> None:
        m = await _make_module(db_session, family=await _make_family(db_session))
        repo = ModuleRepository(db_session)
        out = await repo.get_module(m.id)
        assert out is not None
        assert out.id == m.id


class TestListQuizQuestions:
    async def test_orders_by_question_order_asc(self, db_session: AsyncSession) -> None:
        m = await _make_module(db_session, family=await _make_family(db_session))
        # Insert in reverse order of question_order; reads should reorder.
        for order in (3, 1, 2):
            db_session.add(
                ModuleQuizQuestion(
                    module_id=m.id,
                    question_order=order,
                    question_family_id=uuid4(),
                    question_version=1,
                    question_localized={"bn": f"Q{order}"},
                    options_localized={"bn": ["a", "b", "c", "d"]},
                    correct_indices=[0],
                )
            )

        repo = ModuleRepository(db_session)
        rows = await repo.list_quiz_questions(m.id)
        assert [r.question_order for r in rows] == [1, 2, 3]

    async def test_returns_empty_for_module_with_no_quiz(self, db_session: AsyncSession) -> None:
        m = await _make_module(db_session, family=await _make_family(db_session))
        repo = ModuleRepository(db_session)
        rows = await repo.list_quiz_questions(m.id)
        assert rows == []

    async def test_does_not_return_questions_from_other_modules(self, db_session: AsyncSession) -> None:
        m1 = await _make_module(db_session, family=await _make_family(db_session))
        m2 = await _make_module(db_session, family=await _make_family(db_session))
        db_session.add(
            ModuleQuizQuestion(
                module_id=m1.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "for m1"},
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=m2.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "for m2"},
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_quiz_questions(m1.id)
        assert len(rows) == 1
        assert rows[0].question_localized["bn"] == "for m1"


# ─── search_by_embedding (pgvector cosine distance) ─────────────────────────

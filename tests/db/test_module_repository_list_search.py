"""ModuleRepository — list_modules and search_by_embedding."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from asyncpg import Range
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_repository import (
    ModuleRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.db.conftest import (
    _make_family,
    _make_module,
    _unit_basis_vector,
)

pytestmark = [requires_db, pytest.mark.asyncio]


class TestListModules:
    async def test_default_excludes_retired(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        await _make_module(db_session, family=fam, title_bn="Live", lifecycle_status="published")
        await _make_module(
            db_session,
            family=fam,
            title_bn="Gone",
            lifecycle_status="retired",
            version=2,
            set_family_pointer=False,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules()
        titles = {m.title_bn for m in rows if m.module_family_id == fam.id}
        assert "Live" in titles
        assert "Gone" not in titles

    async def test_status_retired_filter_returns_only_retired(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        await _make_module(db_session, family=fam, title_bn="Live")
        await _make_module(
            db_session,
            family=fam,
            title_bn="Gone",
            lifecycle_status="retired",
            version=2,
            set_family_pointer=False,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(status="retired")
        titles = {m.title_bn for m in rows if m.module_family_id == fam.id}
        assert titles == {"Gone"}

    async def test_clinically_reviewed_true_filter(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        await _make_module(db_session, family=fam, title_bn="Pending", clinically_reviewed=False)
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="Approved",
            clinically_reviewed=True,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(clinically_reviewed=True)
        titles = {m.title_bn for m in rows}
        assert "Approved" in titles
        assert "Pending" not in titles

    async def test_clinically_reviewed_false_filter(self, db_session: AsyncSession) -> None:
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="Approved-only",
            clinically_reviewed=True,
        )
        unique_title = f"Pending-{uuid4().hex[:8]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn=unique_title,
            clinically_reviewed=False,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(clinically_reviewed=False)
        titles = {m.title_bn for m in rows}
        assert unique_title in titles
        assert "Approved-only" not in titles

    async def test_has_visibility_window_true_filter(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        unique_title = f"With-window-{uuid4().hex[:8]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn=unique_title,
            visibility_window=Range(now, now + timedelta(days=14), lower_inc=True, upper_inc=False),
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="No-window",
            visibility_window=None,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(has_visibility_window=True)
        titles = {m.title_bn for m in rows}
        assert unique_title in titles
        assert "No-window" not in titles

    async def test_has_visibility_window_false_filter(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        unique_no = f"No-window-{uuid4().hex[:8]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="With-window",
            visibility_window=Range(now, now + timedelta(days=14), lower_inc=True, upper_inc=False),
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn=unique_no,
            visibility_window=None,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(has_visibility_window=False)
        titles = {m.title_bn for m in rows}
        assert unique_no in titles
        assert "With-window" not in titles

    async def test_domain_filter(self, db_session: AsyncSession) -> None:
        unique_dom = f"ncd-{uuid4().hex[:6]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="NCD-mod",
            domain=unique_dom,
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="RMNCH-mod",
            domain="rmnch",
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(domain=unique_dom)
        assert all(m.domain == unique_dom for m in rows)
        assert any(m.title_bn == "NCD-mod" for m in rows)

    async def test_full_text_query_matches_title_bn(self, db_session: AsyncSession) -> None:
        unique = f"Pregnancy{uuid4().hex[:6]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn=f"Module about {unique}",
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="Module about diabetes",
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(full_text_query=unique)
        assert len(rows) == 1
        assert unique in rows[0].title_bn

    async def test_full_text_query_matches_title_en(self, db_session: AsyncSession) -> None:
        marker = f"FollowUp{uuid4().hex[:6]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="x",
            title_en=f"English {marker} guidance",
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(full_text_query=marker)
        assert len(rows) == 1

    async def test_full_text_query_matches_description_bn(self, db_session: AsyncSession) -> None:
        marker = f"Eclampsia{uuid4().hex[:6]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="hypertension",
            description_bn=f"covers {marker} signs",
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(full_text_query=marker)
        assert len(rows) == 1

    async def test_pagination_offset_and_limit(self, db_session: AsyncSession) -> None:
        # Create a unique domain so we can deterministically isolate this test's rows.
        domain = f"dom-{uuid4().hex[:8]}"
        for i in range(5):
            await _make_module(
                db_session,
                family=await _make_family(db_session),
                title_bn=f"M{i}",
                domain=domain,
                published_at=datetime.now(UTC) + timedelta(seconds=i),
            )

        repo = ModuleRepository(db_session)
        page1 = await repo.list_modules(domain=domain, limit=2, offset=0)
        page2 = await repo.list_modules(domain=domain, limit=2, offset=2)
        page3 = await repo.list_modules(domain=domain, limit=2, offset=4)

        assert len(page1) == 2
        assert len(page2) == 2
        assert len(page3) == 1
        # No row appears on more than one page.
        ids = {m.id for m in page1} | {m.id for m in page2} | {m.id for m in page3}
        assert len(ids) == 5

    async def test_results_ordered_by_published_at_desc(self, db_session: AsyncSession) -> None:
        domain = f"order-{uuid4().hex[:6]}"
        old = datetime.now(UTC) - timedelta(days=1)
        new = datetime.now(UTC)
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="older",
            domain=domain,
            published_at=old,
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="newer",
            domain=domain,
            published_at=new,
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_modules(domain=domain)
        # Newest-first.
        assert rows[0].title_bn == "newer"
        assert rows[1].title_bn == "older"


# ─── get_module / list_quiz_questions ───────────────────────────────────────


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
                    question_bn=f"Q{order}",
                    options_bn=["a", "b", "c", "d"],
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
                question_bn="for m1",
                options_bn=["a", "b", "c", "d"],
                correct_indices=[0],
            )
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=m2.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_bn="for m2",
                options_bn=["a", "b", "c", "d"],
                correct_indices=[0],
            )
        )

        repo = ModuleRepository(db_session)
        rows = await repo.list_quiz_questions(m1.id)
        assert len(rows) == 1
        assert rows[0].question_bn == "for m1"


# ─── search_by_embedding (pgvector cosine distance) ─────────────────────────


class TestSearchByEmbedding:
    async def test_top_k_orders_by_cosine_distance(self, db_session: AsyncSession) -> None:
        # Seed 3 modules with sparse unit vectors on different axes.
        # The query is the unit vector on axis 0 → module A is rank 1.
        a = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="A",
            embedding=_unit_basis_vector(0),
        )
        b = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="B",
            embedding=_unit_basis_vector(1),
        )
        c = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="C",
            embedding=_unit_basis_vector(2),
        )

        repo = ModuleRepository(db_session)
        results = await repo.search_by_embedding(query_vector=_unit_basis_vector(0), limit=3)
        # Filter to only the three rows we seeded so other tests' data
        # doesn't pollute the assertion.
        seeded_ids = {a.id, b.id, c.id}
        ours = [(m, dist) for m, dist in results if m.id in seeded_ids]
        assert len(ours) == 3
        assert ours[0][0].id == a.id, "Module A (same axis as query) should be rank 1"
        # Distance to A should be ~0 (cosine distance of identical vectors).
        assert math.isclose(ours[0][1], 0.0, abs_tol=1e-6)
        # Distance to B and C should be 1.0 (orthogonal).
        for m, dist in ours[1:]:
            assert math.isclose(dist, 1.0, abs_tol=1e-6)

    async def test_skips_modules_without_embedding(self, db_session: AsyncSession) -> None:
        with_emb = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="indexed",
            embedding=_unit_basis_vector(0),
        )
        without_emb = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="not-indexed",
            embedding=None,
        )

        repo = ModuleRepository(db_session)
        results = await repo.search_by_embedding(query_vector=_unit_basis_vector(0), limit=10)
        ids = {m.id for m, _ in results}
        assert with_emb.id in ids
        assert without_emb.id not in ids

    async def test_excludes_retired_even_when_close(self, db_session: AsyncSession) -> None:
        retired = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_bn="retired",
            lifecycle_status="retired",
            embedding=_unit_basis_vector(0),
            set_family_pointer=False,
        )

        repo = ModuleRepository(db_session)
        results = await repo.search_by_embedding(query_vector=_unit_basis_vector(0), limit=10)
        ids = {m.id for m, _ in results}
        assert retired.id not in ids

    async def test_limit_caps_results(self, db_session: AsyncSession) -> None:
        # Seed 5 modules with embeddings; ask for top 2.
        for i in range(5):
            await _make_module(
                db_session,
                family=await _make_family(db_session),
                title_bn=f"M{i}",
                embedding=_unit_basis_vector(i % 10),
            )

        repo = ModuleRepository(db_session)
        results = await repo.search_by_embedding(query_vector=_unit_basis_vector(0), limit=2)
        assert len(results) <= 2


# ─── edit_module: versioning + family pointer + reset flag ─────────────────

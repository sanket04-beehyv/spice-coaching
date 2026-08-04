"""Tests for platform PgVectorStore adapter."""

from __future__ import annotations

import math
from uuid import uuid4

import pytest
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.vectorstore import MODULES_COLLECTION, PgVectorStore
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.db.conftest import _make_family, _make_module, _unit_basis_vector

pytestmark = [requires_db, pytest.mark.asyncio]


class TestPgVectorStore:
    async def test_upsert_and_search_orders_by_distance(self, db_session: AsyncSession) -> None:
        a = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_localized={"bn": "A"},
            embedding=None,
        )
        b = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_localized={"bn": "B"},
            embedding=None,
        )
        store = PgVectorStore(db_session)
        await store.upsert(
            [
                {
                    "collection": MODULES_COLLECTION,
                    "id": str(a.id),
                    "vector": _unit_basis_vector(0),
                },
                {
                    "collection": MODULES_COLLECTION,
                    "id": str(b.id),
                    "vector": _unit_basis_vector(1),
                },
            ]
        )
        await db_session.commit()

        hits = await store.search(
            MODULES_COLLECTION,
            _unit_basis_vector(0),
            top_k=10,
            filters={"lifecycle_status": "published"},
        )
        seeded = [h for h in hits if h["id"] in {str(a.id), str(b.id)}]
        assert seeded[0]["id"] == str(a.id)
        assert math.isclose(seeded[0]["distance"], 0.0, abs_tol=1e-6)

    async def test_delete_nulls_embedding(self, db_session: AsyncSession) -> None:
        module = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_localized={"bn": "Del"},
            embedding=_unit_basis_vector(0),
        )
        store = PgVectorStore(db_session)
        await store.delete(MODULES_COLLECTION, [str(module.id)])
        await db_session.commit()
        await db_session.refresh(module)
        assert module.embedding is None

    async def test_unknown_collection_raises(self, db_session: AsyncSession) -> None:
        store = PgVectorStore(db_session)
        with pytest.raises(ValueError, match="unsupported vector collection"):
            await store.upsert([{"collection": "cards", "id": str(uuid4()), "vector": _unit_basis_vector(0)}])
        with pytest.raises(ValueError, match="unsupported vector collection"):
            await store.search("cards", _unit_basis_vector(0), top_k=1)

    async def test_search_by_embedding_delegates_to_store(self, db_session: AsyncSession) -> None:
        a = await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_localized={"bn": "A"},
            embedding=_unit_basis_vector(0),
        )
        repo = ModuleRepository(db_session)
        results = await repo.search_by_embedding(query_vector=_unit_basis_vector(0), limit=5)
        ours = [(m, d) for m, d in results if m.id == a.id]
        assert len(ours) == 1
        assert math.isclose(ours[0][1], 0.0, abs_tol=1e-6)

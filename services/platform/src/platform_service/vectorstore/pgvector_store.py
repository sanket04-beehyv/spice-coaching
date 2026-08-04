"""Postgres / pgvector implementation of ``mc_foundation.vectorstore.VectorStore``.

Collection ``modules`` maps to ``Module.embedding``. Search filter keys
(interpreted only here — not in foundation):

- ``lifecycle_status`` (str) — defaults to ``\"published\"`` when omitted
- ``tenant_id`` (UUID or str) — optional tenant scope
- ``assignable_only`` (bool) — when true, restrict to training module families
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from mc_foundation.vectorstore import VectorMatch, VectorRecord
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.module_availability import is_training_module_family
from platform_service.db.tenant_scope import tenant_scope_filter

MODULES_COLLECTION = "modules"


class PgVectorStore:
    """Durable vectors co-located on Postgres via the pgvector extension."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            collection = record["collection"]
            if collection != MODULES_COLLECTION:
                raise ValueError(f"unsupported vector collection: {collection!r}")
            module_id = UUID(record["id"])
            module = await self._session.get(Module, module_id)
            if module is None:
                continue
            module.embedding = list(record["vector"])

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        if collection != MODULES_COLLECTION:
            raise ValueError(f"unsupported vector collection: {collection!r}")
        if not ids:
            return
        module_ids = [UUID(item_id) for item_id in ids]
        await self._session.execute(update(Module).where(Module.id.in_(module_ids)).values(embedding=None))

    async def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
        filters: Mapping[str, object] | None = None,
    ) -> Sequence[VectorMatch]:
        if collection != MODULES_COLLECTION:
            raise ValueError(f"unsupported vector collection: {collection!r}")
        if top_k <= 0:
            return []

        filters = filters or {}
        lifecycle_status = filters.get("lifecycle_status", "published")
        if not isinstance(lifecycle_status, str):
            raise ValueError("filters.lifecycle_status must be a str when provided")

        assignable_only = bool(filters.get("assignable_only", False))
        tenant_id = _parse_optional_tenant_id(filters.get("tenant_id"))

        distance = Module.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt = (
            select(Module.id, distance)
            .where(Module.embedding.is_not(None), Module.lifecycle_status == lifecycle_status)
            .order_by(distance.asc())
            .limit(top_k)
        )
        if assignable_only:
            stmt = stmt.where(is_training_module_family())
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))

        rows = (await self._session.execute(stmt)).all()
        return [{"id": str(module_id), "distance": float(dist)} for module_id, dist in rows]


def _parse_optional_tenant_id(raw: object | None) -> UUID | None:
    if raw is None:
        return None
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        return UUID(raw)
    raise ValueError("filters.tenant_id must be a UUID or str when provided")

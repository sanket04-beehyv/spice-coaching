"""Embedding retrieval via ai-runtime + pgvector cosine distance."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.exceptions import EmbeddingDimensionError
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.embedding_vector import assert_embedding_dimension
from platform_service.services.module_search_text import module_text_for_search

from eval.rag.corpus import (
    _title_parts_from_localized,
    count_embedded_published_modules,
    load_cards_by_module_ids,
)


@dataclass(frozen=True)
class EmbeddingHit:
    rank: int
    module_id: UUID
    primary_title: str | None
    title_en: str | None
    title_bn: str | None
    cosine_distance: float
    text_preview: str


class EmbeddingRetriever:
    """Embed queries via ai-runtime and search published modules by cosine distance."""

    def __init__(
        self,
        *,
        tenant_id: UUID | None = None,
        client: AIRuntimeClient | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._settings = get_settings()
        self._client = client or AIRuntimeClient(base_url=base_url, token=token)
        self._owns_client = client is None
        self._embedded_count: int | None = None

    async def embedded_count(self) -> int:
        if self._embedded_count is None:
            self._embedded_count = await count_embedded_published_modules(tenant_id=self._tenant_id)
        return self._embedded_count

    async def search(self, query: str, *, k: int) -> list[EmbeddingHit]:
        vectors = await self._client.embed([query])
        if not vectors:
            raise RuntimeError("ai-runtime returned no embedding for query")

        try:
            vec = assert_embedding_dimension(vectors[0], expected_dim=self._settings.embedding_dimension)
        except EmbeddingDimensionError as exc:
            raise RuntimeError(str(exc)) from exc

        async with SessionLocal() as session:
            repo = ModuleReadRepository(session)
            pairs = await repo.search_by_embedding(
                query_vector=vec,
                limit=k,
                tenant_id=self._tenant_id,
            )
            cards_by_module = await load_cards_by_module_ids([module.id for module, _ in pairs])

        hits: list[EmbeddingHit] = []
        for rank, (module, distance) in enumerate(pairs, start=1):
            preview = module_text_for_search(
                module,
                cards=cards_by_module.get(module.id, []),
            )[:120].replace("\n", " ")
            primary_title, title_en, title_bn = _title_parts_from_localized(module.title_localized)
            hits.append(
                EmbeddingHit(
                    rank=rank,
                    module_id=module.id,
                    primary_title=primary_title,
                    title_en=title_en,
                    title_bn=title_bn,
                    cosine_distance=float(distance),
                    text_preview=preview,
                )
            )
        return hits

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

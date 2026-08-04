"""Unit tests for mc_foundation.vectorstore."""

from __future__ import annotations

import pytest
from mc_foundation.vectorstore import InMemoryVectorStore, cosine_distance


class TestCosineDistance:
    def test_identical_unit_vectors(self) -> None:
        assert cosine_distance([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            cosine_distance([1.0], [1.0, 0.0])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            cosine_distance([], [])


class TestInMemoryVectorStore:
    @pytest.mark.asyncio
    async def test_upsert_search_orders_by_distance(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            [
                {"collection": "modules", "id": "a", "vector": [1.0, 0.0]},
                {"collection": "modules", "id": "b", "vector": [0.0, 1.0]},
                {"collection": "modules", "id": "c", "vector": [0.9, 0.1]},
            ]
        )
        hits = await store.search("modules", [1.0, 0.0], top_k=2)
        assert [h["id"] for h in hits] == ["a", "c"]
        assert hits[0]["distance"] == pytest.approx(0.0)
        assert hits[1]["distance"] > hits[0]["distance"]

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert([{"collection": "modules", "id": "a", "vector": [1.0, 0.0]}])
        await store.upsert([{"collection": "modules", "id": "a", "vector": [0.0, 1.0]}])
        hits = await store.search("modules", [0.0, 1.0], top_k=1)
        assert hits[0]["id"] == "a"
        assert hits[0]["distance"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_delete_removes_vector(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            [
                {"collection": "modules", "id": "a", "vector": [1.0, 0.0]},
                {"collection": "modules", "id": "b", "vector": [0.0, 1.0]},
            ]
        )
        await store.delete("modules", ["a"])
        hits = await store.search("modules", [1.0, 0.0], top_k=10)
        assert [h["id"] for h in hits] == ["b"]

    @pytest.mark.asyncio
    async def test_search_isolates_collections(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            [
                {"collection": "modules", "id": "a", "vector": [1.0, 0.0]},
                {"collection": "cards", "id": "a", "vector": [1.0, 0.0]},
            ]
        )
        hits = await store.search("cards", [1.0, 0.0], top_k=10)
        assert [h["id"] for h in hits] == ["a"]
        assert len(hits) == 1

    @pytest.mark.asyncio
    async def test_filters_match_metadata(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert(
            [
                {
                    "collection": "modules",
                    "id": "pub",
                    "vector": [1.0, 0.0],
                    "metadata": {"lifecycle_status": "published"},
                },
                {
                    "collection": "modules",
                    "id": "draft",
                    "vector": [1.0, 0.0],
                    "metadata": {"lifecycle_status": "draft"},
                },
            ]
        )
        hits = await store.search(
            "modules",
            [1.0, 0.0],
            top_k=10,
            filters={"lifecycle_status": "published"},
        )
        assert [h["id"] for h in hits] == ["pub"]

    @pytest.mark.asyncio
    async def test_top_k_zero_returns_empty(self) -> None:
        store = InMemoryVectorStore()
        await store.upsert([{"collection": "modules", "id": "a", "vector": [1.0, 0.0]}])
        assert await store.search("modules", [1.0, 0.0], top_k=0) == []

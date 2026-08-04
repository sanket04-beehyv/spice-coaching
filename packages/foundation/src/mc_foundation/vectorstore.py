"""Vendor-agnostic vector store protocol and test double.

Production adapters (e.g. pgvector) live in the consuming service. This
module stays free of SQLAlchemy, domain models, and vendor SDKs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import NotRequired, Protocol, TypedDict


class VectorRecord(TypedDict):
    """One vector to upsert into a named collection."""

    collection: str
    id: str
    vector: list[float]
    metadata: NotRequired[Mapping[str, object]]


class VectorMatch(TypedDict):
    """A similarity hit. ``distance`` is cosine distance (lower = more similar)."""

    id: str
    distance: float


class VectorStore(Protocol):
    """Minimal durable vector upsert / delete / search surface."""

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        """Insert or replace vectors for the given records."""
        ...

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        """Remove vectors by id within a collection (missing ids are ignored)."""
        ...

    async def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
        filters: Mapping[str, object] | None = None,
    ) -> Sequence[VectorMatch]:
        """Return up to ``top_k`` matches ordered by ascending cosine distance.

        ``filters`` is an opaque map interpreted by the adapter. Foundation
        does not define domain filter keys.
        """
        ...


def cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance in ``[0, 2]`` (``1 - cosine_similarity``)."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    if not a:
        raise ValueError("vectors must be non-empty")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    similarity = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    # Clamp floating-point noise outside [-1, 1].
    similarity = max(-1.0, min(1.0, similarity))
    return 1.0 - similarity


class InMemoryVectorStore:
    """Brute-force in-memory store for unit tests and wiring checks.

    Supports optional equality filters against record ``metadata`` keys.
    Not for production use.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], VectorRecord] = {}

    async def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            stored: VectorRecord = {
                "collection": record["collection"],
                "id": record["id"],
                "vector": list(record["vector"]),
            }
            if "metadata" in record:
                stored["metadata"] = dict(record["metadata"])
            self._records[(record["collection"], record["id"])] = stored

    async def delete(self, collection: str, ids: Sequence[str]) -> None:
        for item_id in ids:
            self._records.pop((collection, item_id), None)

    async def search(
        self,
        collection: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
        filters: Mapping[str, object] | None = None,
    ) -> Sequence[VectorMatch]:
        if top_k <= 0:
            return []
        matches: list[VectorMatch] = []
        for (coll, item_id), record in self._records.items():
            if coll != collection:
                continue
            if filters and not _metadata_matches(record.get("metadata"), filters):
                continue
            matches.append(
                {
                    "id": item_id,
                    "distance": cosine_distance(query_vector, record["vector"]),
                }
            )
        matches.sort(key=lambda m: (m["distance"], m["id"]))
        return matches[:top_k]


def _metadata_matches(
    metadata: Mapping[str, object] | None,
    filters: Mapping[str, object],
) -> bool:
    if not filters:
        return True
    if metadata is None:
        return False
    for key, expected in filters.items():
        if metadata.get(key) != expected:
            return False
    return True


__all__ = [
    "InMemoryVectorStore",
    "VectorMatch",
    "VectorRecord",
    "VectorStore",
    "cosine_distance",
]

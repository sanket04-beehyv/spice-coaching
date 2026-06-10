"""Shared helpers for ModuleRepository tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from asyncpg import Range
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from sqlalchemy.ext.asyncio import AsyncSession

# ─── Helpers ────────────────────────────────────────────────────────────────


def _zero_vector(dim: int = 768) -> list[float]:
    return [0.0] * dim


def _unit_basis_vector(axis: int, dim: int = 768) -> list[float]:
    """Sparse unit vector — zero everywhere except `axis`. Cosine-distance
    between two of these is 1.0 if axes differ, 0.0 if same axis."""
    v = [0.0] * dim
    v[axis % dim] = 1.0
    return v


async def _make_family(
    session: AsyncSession,
    *,
    module_code: str | None = None,
) -> ModuleFamily:
    fam = ModuleFamily(module_code=module_code or f"family-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    return fam


async def _make_module(
    session: AsyncSession,
    *,
    family: ModuleFamily | None = None,
    title_bn: str = "Sample Module",
    title_en: str | None = None,
    description_bn: str | None = "Description",
    domain: str = "rmnch",
    module_type: str = "refresher",
    lifecycle_status: str = "published",
    clinically_reviewed: bool = False,
    visibility_window: Range | None = None,
    embedding: list[float] | None = None,
    module_json: dict[str, Any] | None = None,
    thumbnail_storage_path: str | None = None,
    version: int = 1,
    published_at: datetime | None = None,
    set_family_pointer: bool = True,
) -> Module:
    if family is None:
        family = await _make_family(session)
    module = Module(
        module_family_id=family.id,
        version=version,
        title_bn=title_bn,
        title_en=title_en,
        description_bn=description_bn,
        domain=domain,
        module_type=module_type,
        lifecycle_status=lifecycle_status,
        clinically_reviewed=clinically_reviewed,
        visibility_window=visibility_window,
        embedding=embedding,
        module_json=module_json or {"cards": [{"title_bn": "Card 1"}]},
        thumbnail_storage_path=thumbnail_storage_path,
        published_at=published_at or (datetime.now(UTC) if lifecycle_status == "published" else None),
    )
    session.add(module)
    await session.flush()
    if set_family_pointer and lifecycle_status == "published":
        family.current_published_module_id = module.id
        await session.flush()
    return module


# ─── list_modules: filters + ordering ───────────────────────────────────────

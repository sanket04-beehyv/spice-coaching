"""Load published modules from the database into a searchable corpus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mc_foundation.locale import LOCALIZED_CARD_TEXT_FIELDS
from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.db.tenant_scope import tenant_scope_filter
from platform_service.localized import primary_text
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.module_search_text import (
    card_metadata_text_for_search,
    module_text_for_search,
)
from sqlalchemy import func, select


@dataclass(frozen=True)
class CorpusDoc:
    module_id: UUID
    primary_title: str | None
    title_en: str | None
    title_bn: str | None
    text: str


@dataclass(frozen=True)
class CardCorpusDoc:
    module_id: UUID
    card_id: UUID
    card_index: int
    card_family_id: UUID | None
    primary_title: str | None
    title_en: str | None
    title_bn: str | None
    text: str


def lookup_card_by_id(
    card_id: UUID,
    module_ids: list[UUID],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> CardCorpusDoc | None:
    """Find a card document by ``module_card.id`` across the given modules."""
    for module_id in module_ids:
        for card in cards_by_module.get(module_id, []):
            if card.card_id == card_id:
                return card
    return None


def _title_parts_from_localized(
    localized: dict[str, Any] | None,
    *,
    legacy_en: str | None = None,
    legacy_bn: str | None = None,
    primary: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return (primary_title, title_en, title_bn) from a locale map or legacy suffix keys."""
    primary_locale = primary or get_settings().deployment_primary_locale
    if isinstance(localized, dict):
        primary_title = (localized.get(primary_locale) or "").strip() or None
        en = (localized.get("en") or "").strip() or None
        bn = (localized.get("bn") or "").strip() or None
        return primary_title, en, bn
    en = (legacy_en or "").strip() or None
    bn = (legacy_bn or "").strip() or None
    primary_title = bn or en
    return primary_title, en, bn


def _localized_field_text(card: dict[str, Any], field: str) -> list[str]:
    """Extract searchable text for one localized card field (with legacy fallback)."""
    value = card.get(field)
    if value is None:
        legacy_bn = card.get(f"{field}_bn")
        legacy_en = card.get(f"{field}_en")
        if legacy_bn is not None or legacy_en is not None:
            value = {}
            if legacy_bn is not None:
                value["bn"] = legacy_bn
            if legacy_en is not None:
                value["en"] = legacy_en
    if not value:
        return []
    if field == "body":
        parts: list[str] = []
        if isinstance(value, dict):
            for locale_value in value.values():
                text = card_body_plain_text(locale_value)
                if text:
                    parts.append(text)
        else:
            text = card_body_plain_text(value)
            if text:
                parts.append(text)
        return parts
    if isinstance(value, dict):
        primary = primary_text(value)
        if primary:
            return [primary]
        return []
    return [str(value)]


def _card_title_parts(card: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    title = card.get("title")
    if isinstance(title, dict):
        return _title_parts_from_localized(title)
    return _title_parts_from_localized(
        None,
        legacy_en=card.get("title_en"),
        legacy_bn=card.get("title_bn"),
    )


def card_text_for_search(card: dict[str, Any]) -> str:
    """Concatenate one card's searchable fields (no module-level metadata)."""
    parts: list[str] = []
    for field in LOCALIZED_CARD_TEXT_FIELDS:
        parts.extend(_localized_field_text(card, field))
    parts.extend(card_metadata_text_for_search(card.get("search_metadata")))
    return "\n".join(parts)


def build_module_card_corpus(
    modules: list[Module],
    cards_by_module: dict[UUID, list[dict[str, Any]]],
) -> dict[UUID, list[CardCorpusDoc]]:
    """Return searchable card documents grouped by module ID."""
    cards_by_module_out: dict[UUID, list[CardCorpusDoc]] = {}
    for module in modules:
        cards = cards_by_module.get(module.id, [])
        module_cards: list[CardCorpusDoc] = []
        for card_index, card in enumerate(cards):
            if not isinstance(card, dict):
                continue
            text = card_text_for_search(card)
            if not text.strip():
                continue
            primary_title, title_en, title_bn = _card_title_parts(card)
            card_id_raw = card.get("id")
            if card_id_raw is None:
                continue
            family_raw = card.get("card_family_id")
            card_family_id = UUID(str(family_raw)) if family_raw else None
            module_cards.append(
                CardCorpusDoc(
                    module_id=module.id,
                    card_id=UUID(str(card_id_raw)),
                    card_index=card_index,
                    card_family_id=card_family_id,
                    primary_title=primary_title,
                    title_en=title_en,
                    title_bn=title_bn,
                    text=text,
                )
            )
        if module_cards:
            cards_by_module_out[module.id] = module_cards
    return cards_by_module_out


async def load_cards_by_module_ids(module_ids: list[UUID]) -> dict[UUID, list[dict[str, Any]]]:
    if not module_ids:
        return {}
    async with SessionLocal() as session:
        repo = ModuleReadRepository(session)
        rows = await repo.list_cards_for_module_ids(module_ids)
    cards_by_module: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        if row.module_id is None:
            continue
        cards_by_module.setdefault(row.module_id, []).append(card_row_to_dict(row))
    return cards_by_module


async def load_published_modules(*, tenant_id: UUID | None = None) -> list[Module]:
    async with SessionLocal() as session:
        repo = ModuleReadRepository(session)
        return await repo.list_modules(status="published", limit=10_000, tenant_id=tenant_id)


def corpus_docs_from_modules(
    modules: list[Module],
    cards_by_module: dict[UUID, list[dict[str, Any]]] | None = None,
) -> list[CorpusDoc]:
    docs: list[CorpusDoc] = []
    for module in modules:
        cards = (cards_by_module or {}).get(module.id, [])
        text = module_text_for_search(module, cards=cards)
        if not text.strip():
            continue
        primary_title, title_en, title_bn = _title_parts_from_localized(module.title_localized)
        docs.append(
            CorpusDoc(
                module_id=module.id,
                primary_title=primary_title,
                title_en=title_en,
                title_bn=title_bn,
                text=text,
            )
        )
    return docs


async def load_published_corpus(*, tenant_id: UUID | None = None) -> list[CorpusDoc]:
    """Return one searchable document per published module."""
    modules = await load_published_modules(tenant_id=tenant_id)
    cards_by_module = await load_cards_by_module_ids([module.id for module in modules])
    return corpus_docs_from_modules(modules, cards_by_module)


async def count_embedded_published_modules(*, tenant_id: UUID | None = None) -> int:
    """Count published modules with a non-null embedding vector."""
    async with SessionLocal() as session:
        stmt = (
            select(func.count())
            .select_from(Module)
            .where(Module.embedding.is_not(None), Module.lifecycle_status == "published")
        )
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        result = await session.execute(stmt)
        return int(result.scalar_one())

"""Normalize module cards into module_card table.

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-03

Backfills ``module_json.cards`` into relational ``module_card`` rows (minting
fresh ``card_family_id`` per card — none existed in persisted JSON), then
removes the ``cards`` key from ``module_json``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CARD_TEXT_FIELDS = (
    "title",
    "body",
    "previous_practice",
    "current_practice",
    "rationale_for_change",
    "next_action",
)
_LEGACY_PRIMARY_SUFFIX = "_bn"
_LEGACY_MIRROR_SUFFIX = "_en"


def _build_localized(bn: str | None, en: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if bn and str(bn).strip():
        out["bn"] = str(bn).strip()
    if en and str(en).strip():
        out["en"] = str(en).strip()
    return out


def _migrate_suffix_field(data: dict[str, Any], field: str) -> None:
    if field in data:
        return
    bn_key = f"{field}{_LEGACY_PRIMARY_SUFFIX}"
    en_key = f"{field}{_LEGACY_MIRROR_SUFFIX}"
    bn_val = data.pop(bn_key, None)
    en_val = data.pop(en_key, None)
    if bn_val is None and en_val is None:
        return
    data[field] = _build_localized(
        bn_val if isinstance(bn_val, str) else None,
        en_val if isinstance(en_val, str) else None,
    )


def _migrate_card(card: dict[str, Any]) -> dict[str, Any]:
    out = dict(card)
    for field in _CARD_TEXT_FIELDS:
        _migrate_suffix_field(out, field)
    return out


def _parse_uuid_list(raw: Any) -> list[uuid.UUID] | None:
    if not raw:
        return None
    if not isinstance(raw, list):
        return None
    out: list[uuid.UUID] = []
    for item in raw:
        try:
            out.append(uuid.UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return out or None


def _parse_uuid(raw: Any) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _localized_field(card: dict[str, Any], field: str) -> dict[str, Any] | None:
    value = card.get(field)
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"bn": value}
    return None


def _backfill_module_cards() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, module_json FROM module")).fetchall()
    for module_id, module_json in rows:
        if not isinstance(module_json, dict):
            continue
        cards = module_json.get("cards")
        if not isinstance(cards, list) or not cards:
            continue
        for order, raw_card in enumerate(cards, start=1):
            if not isinstance(raw_card, dict):
                continue
            card = _migrate_card(raw_card)
            title = _localized_field(card, "title")
            if not title:
                continue
            row_id = uuid.uuid4()
            family_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    """
                    INSERT INTO module_card (
                        id, module_id, card_order, card_family_id, card_version,
                        title_localized, body_localized,
                        previous_practice_localized, current_practice_localized,
                        rationale_for_change_localized, next_action_localized,
                        thresholds_jsonb, source_block_ids, figure_ref_block_id,
                        search_metadata_jsonb, attachments_jsonb, field_flags_jsonb
                    ) VALUES (
                        CAST(:id AS uuid), CAST(:module_id AS uuid), :card_order,
                        CAST(:card_family_id AS uuid), 1,
                        CAST(:title_localized AS jsonb),
                        CAST(:body_localized AS jsonb),
                        CAST(:previous_practice_localized AS jsonb),
                        CAST(:current_practice_localized AS jsonb),
                        CAST(:rationale_for_change_localized AS jsonb),
                        CAST(:next_action_localized AS jsonb),
                        CAST(:thresholds_jsonb AS jsonb),
                        CAST(:source_block_ids AS uuid[]),
                        CAST(:figure_ref_block_id AS uuid),
                        CAST(:search_metadata_jsonb AS jsonb),
                        CAST(:attachments_jsonb AS jsonb),
                        CAST(:field_flags_jsonb AS jsonb)
                    )
                    """
                ),
                {
                    "id": str(row_id),
                    "module_id": str(module_id),
                    "card_order": order,
                    "card_family_id": str(family_id),
                    "title_localized": json.dumps(title),
                    "body_localized": json.dumps(_localized_field(card, "body"))
                    if _localized_field(card, "body") is not None
                    else None,
                    "previous_practice_localized": json.dumps(_localized_field(card, "previous_practice"))
                    if _localized_field(card, "previous_practice") is not None
                    else None,
                    "current_practice_localized": json.dumps(_localized_field(card, "current_practice"))
                    if _localized_field(card, "current_practice") is not None
                    else None,
                    "rationale_for_change_localized": json.dumps(
                        _localized_field(card, "rationale_for_change")
                    )
                    if _localized_field(card, "rationale_for_change") is not None
                    else None,
                    "next_action_localized": json.dumps(_localized_field(card, "next_action"))
                    if _localized_field(card, "next_action") is not None
                    else None,
                    "thresholds_jsonb": json.dumps(card.get("thresholds"))
                    if card.get("thresholds") is not None
                    else None,
                    "source_block_ids": _parse_uuid_list(card.get("source_block_ids")),
                    "figure_ref_block_id": _parse_uuid(card.get("figure_ref_block_id")),
                    "search_metadata_jsonb": json.dumps(card.get("search_metadata"))
                    if card.get("search_metadata") is not None
                    else None,
                    "attachments_jsonb": json.dumps(card.get("attachments"))
                    if card.get("attachments") is not None
                    else None,
                    "field_flags_jsonb": json.dumps(card.get("field_flags_jsonb") or card.get("field_flags"))
                    if card.get("field_flags_jsonb") is not None or card.get("field_flags") is not None
                    else None,
                },
            )


def _strip_cards_from_module_json() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE module
            SET module_json = CASE
                WHEN module_json - 'cards' = '{}'::jsonb THEN NULL
                ELSE module_json - 'cards'
            END
            WHERE module_json ? 'cards'
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "module_card",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("module_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("card_order", sa.Integer(), nullable=False),
        sa.Column("card_family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("card_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title_localized", postgresql.JSONB(), nullable=False),
        sa.Column("body_localized", postgresql.JSONB(), nullable=True),
        sa.Column("previous_practice_localized", postgresql.JSONB(), nullable=True),
        sa.Column("current_practice_localized", postgresql.JSONB(), nullable=True),
        sa.Column("rationale_for_change_localized", postgresql.JSONB(), nullable=True),
        sa.Column("next_action_localized", postgresql.JSONB(), nullable=True),
        sa.Column("thresholds_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column("source_block_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
        sa.Column("figure_ref_block_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("search_metadata_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column("attachments_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column("field_flags_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["module_id"], ["module.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_family_id", "card_version", name="uq_module_card_family_version"),
    )
    op.create_index("ix_module_card_module_id", "module_card", ["module_id"])

    _backfill_module_cards()
    _strip_cards_from_module_json()


def downgrade() -> None:
    conn = op.get_bind()
    modules = conn.execute(sa.text("SELECT DISTINCT module_id FROM module_card")).fetchall()
    for (module_id,) in modules:
        rows = conn.execute(
            sa.text(
                """
                SELECT card_order, title_localized, body_localized,
                       previous_practice_localized, current_practice_localized,
                       rationale_for_change_localized, next_action_localized,
                       thresholds_jsonb, source_block_ids, figure_ref_block_id,
                       search_metadata_jsonb, attachments_jsonb, field_flags_jsonb,
                       card_family_id
                FROM module_card
                WHERE module_id = CAST(:module_id AS uuid)
                ORDER BY card_order ASC
                """
            ),
            {"module_id": str(module_id)},
        ).fetchall()
        cards: list[dict[str, Any]] = []
        for row in rows:
            card: dict[str, Any] = {
                "title": row.title_localized,
                "card_family_id": str(row.card_family_id),
            }
            if row.body_localized is not None:
                card["body"] = row.body_localized
            if row.previous_practice_localized is not None:
                card["previous_practice"] = row.previous_practice_localized
            if row.current_practice_localized is not None:
                card["current_practice"] = row.current_practice_localized
            if row.rationale_for_change_localized is not None:
                card["rationale_for_change"] = row.rationale_for_change_localized
            if row.next_action_localized is not None:
                card["next_action"] = row.next_action_localized
            if row.thresholds_jsonb is not None:
                card["thresholds"] = row.thresholds_jsonb
            if row.source_block_ids:
                card["source_block_ids"] = [str(bid) for bid in row.source_block_ids]
            if row.figure_ref_block_id is not None:
                card["figure_ref_block_id"] = str(row.figure_ref_block_id)
            if row.search_metadata_jsonb is not None:
                card["search_metadata"] = row.search_metadata_jsonb
            if row.attachments_jsonb is not None:
                card["attachments"] = row.attachments_jsonb
            if row.field_flags_jsonb is not None:
                card["field_flags_jsonb"] = row.field_flags_jsonb
            cards.append(card)
        conn.execute(
            sa.text(
                """
                UPDATE module
                SET module_json = COALESCE(module_json, '{}'::jsonb) || CAST(:cards_patch AS jsonb)
                WHERE id = CAST(:module_id AS uuid)
                """
            ),
            {
                "module_id": str(module_id),
                "cards_patch": json.dumps({"cards": cards}),
            },
        )

    op.drop_index("ix_module_card_module_id", table_name="module_card")
    op.drop_table("module_card")

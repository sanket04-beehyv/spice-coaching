"""Migrate bilingual *_bn/*_en columns to locale-keyed JSONB maps.

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-26

Relational columns become ``*_localized`` JSONB maps keyed by locale code
(legacy data uses ``bn`` / ``en`` keys). ``module_json`` card payloads are
transformed in-place by the same migration via a Python data pass.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
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
_SEARCH_META_LIST_FIELDS = (
    "keywords",
    "search_phrases",
    "retrieval_hints",
    "questions",
    "topic_tags",
)


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
    bn_key = f"{field}_bn"
    en_key = f"{field}_en"
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
    sm = out.get("search_metadata")
    if isinstance(sm, dict):
        sm_out = dict(sm)
        for field in _SEARCH_META_LIST_FIELDS:
            _migrate_suffix_field(sm_out, field)
        out["search_metadata"] = sm_out
    return out


def _migrate_module_json(module_json: Any) -> Any:
    if not isinstance(module_json, dict):
        return module_json
    out = dict(module_json)
    cards = out.get("cards")
    if isinstance(cards, list):
        out["cards"] = [_migrate_card(c) for c in cards if isinstance(c, dict)]
    return out


def _migrate_search_metadata(metadata: Any) -> Any:
    if not isinstance(metadata, dict):
        return metadata
    out = dict(metadata)
    for field in _SEARCH_META_LIST_FIELDS:
        _migrate_suffix_field(out, field)
    return out


def _backfill_module_json() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, module_json, search_metadata_jsonb FROM module")
    ).fetchall()
    for row in rows:
        module_id, module_json, search_metadata = row
        new_module_json = _migrate_module_json(module_json)
        new_search_metadata = _migrate_search_metadata(search_metadata)
        conn.execute(
            sa.text(
                "UPDATE module SET module_json = CAST(:module_json AS jsonb), "
                "search_metadata_jsonb = CAST(:search_metadata AS jsonb) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": str(module_id),
                "module_json": json.dumps(new_module_json) if new_module_json is not None else None,
                "search_metadata": json.dumps(new_search_metadata)
                if new_search_metadata is not None
                else None,
            },
        )


def upgrade() -> None:
    # ── module ──────────────────────────────────────────────────────────
    op.add_column("module", sa.Column("title_localized", postgresql.JSONB, nullable=True))
    op.add_column("module", sa.Column("description_localized", postgresql.JSONB, nullable=True))
    op.execute(
        """
        UPDATE module SET
          title_localized = jsonb_strip_nulls(jsonb_build_object('bn', title_bn, 'en', title_en)),
          description_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', description_bn, 'en', description_en)
          )
        """
    )
    op.alter_column("module", "title_localized", nullable=False)
    op.drop_column("module", "title_en")
    op.drop_column("module", "title_bn")
    op.drop_column("module", "description_en")
    op.drop_column("module", "description_bn")

    _backfill_module_json()

    # ── module_quiz_question ────────────────────────────────────────────
    op.add_column(
        "module_quiz_question",
        sa.Column("question_localized", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "module_quiz_question",
        sa.Column("case_setup_localized", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "module_quiz_question",
        sa.Column("options_localized", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "module_quiz_question",
        sa.Column("explanation_localized", postgresql.JSONB, nullable=True),
    )
    op.execute(
        """
        UPDATE module_quiz_question SET
          question_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', question_bn, 'en', question_en)
          ),
          case_setup_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', case_setup_bn, 'en', case_setup_en)
          ),
          options_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', options_bn, 'en', options_en)
          ),
          explanation_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', explanation_bn, 'en', explanation_en)
          )
        """
    )
    op.alter_column("module_quiz_question", "question_localized", nullable=False)
    op.alter_column("module_quiz_question", "options_localized", nullable=False)
    op.drop_column("module_quiz_question", "question_en")
    op.drop_column("module_quiz_question", "question_bn")
    op.drop_column("module_quiz_question", "case_setup_en")
    op.drop_column("module_quiz_question", "case_setup_bn")
    op.drop_column("module_quiz_question", "options_en")
    op.drop_column("module_quiz_question", "options_bn")
    op.drop_column("module_quiz_question", "explanation_en")
    op.drop_column("module_quiz_question", "explanation_bn")

    # ── chat_frequent_question ──────────────────────────────────────────
    op.add_column(
        "chat_frequent_question",
        sa.Column("question_localized", postgresql.JSONB, nullable=True),
    )
    op.execute(
        """
        UPDATE chat_frequent_question SET
          question_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', question_bn, 'en', question_en)
          )
        """
    )
    op.alter_column("chat_frequent_question", "question_localized", nullable=False)
    op.drop_column("chat_frequent_question", "question_en")
    op.drop_column("chat_frequent_question", "question_bn")

    # ── module_candidate_draft ──────────────────────────────────────────
    op.add_column(
        "module_candidate_draft",
        sa.Column("description_localized", postgresql.JSONB, nullable=True),
    )
    op.execute(
        """
        UPDATE module_candidate_draft SET
          description_localized = jsonb_strip_nulls(
            jsonb_build_object('bn', description_bn, 'en', description_en)
          )
        """
    )
    op.drop_column("module_candidate_draft", "description_en")
    op.drop_column("module_candidate_draft", "description_bn")

    # source_document.primary_language: drop server default 'bn' (deployment-driven)
    op.alter_column("source_document", "primary_language", server_default=None)


def downgrade() -> None:
    raise NotImplementedError("0030_localized_content downgrade is not supported")

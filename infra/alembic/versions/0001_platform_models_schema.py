"""Platform models schema (squashed).

Revision ID: 0001
Revises:
Create Date: 2026-05-11

This migration creates the schema described by `infra/sql/platform_models_schema.sql`
on an empty database (extensions + tables + constraints + indexes).
"""

# pylint: disable=no-member

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions used by the standalone schema.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ──────────────────────────────────────────────────────────────────────────
    # Source layer
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "source_document",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_document_family_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("primary_language", sa.Text(), nullable=False, server_default="bn"),
        sa.Column(
            "authority_kind",
            sa.Text(),
            nullable=False,
            server_default="official_training",
        ),
        sa.Column("authority_label", sa.Text(), nullable=False),
        sa.Column("version_label", sa.Text(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("original_storage_path", sa.Text(), nullable=False),
        sa.Column("outline_method", sa.Text(), nullable=True),
        sa.Column("outline_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("extraction_calibration_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="ingesting"),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ingested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "source_page",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_image_path", sa.Text(), nullable=True),
        sa.Column("markdown_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("extraction_method", sa.Text(), nullable=False, server_default="text"),
        sa.Column(
            "extraction_quality_score",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
        ),
        sa.Column("text_extraction_alt", sa.Text(), nullable=True),
        sa.Column("language_detected", sa.Text(), nullable=True),
        sa.Column("metadata_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "page_number",
            name="uq_source_page_doc_page",
        ),
    )

    op.create_table(
        "content_block",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_page.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("block_order", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.Text(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_language", sa.Text(), nullable=True),
        sa.Column("heading_path_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ingestion_run",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("error_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("triggered_by", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.create_table(
        "ingestion_run_step",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("input_summary_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("output_summary_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("llm_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_jsonb", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "llm_call_cache",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("input_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("response_jsonb", postgresql.JSONB, nullable=False),
        sa.Column("token_usage_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "module_candidate_draft",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ingestion_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposed_title", sa.Text(), nullable=False),
        sa.Column("behavioural_gap_code", sa.Text(), nullable=True),
        sa.Column("scope_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source_provenance_jsonb",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("estimated_card_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_quiz_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clinical_review_notes", sa.Text(), nullable=True),
        sa.Column("proposed_module_type", sa.Text(), nullable=False, server_default="refresher"),
        sa.Column("previous_practice_summary", sa.Text(), nullable=True),
        sa.Column("current_practice_summary", sa.Text(), nullable=True),
        sa.Column("rationale_summary", sa.Text(), nullable=True),
        sa.Column("quality_flags_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Module layer
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "module_family",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("module_code", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_published_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # NOTE: The standalone schema intentionally does not declare a SQL-level FK
    # for module.module_family_id. This migration mirrors that.
    op.create_table(
        "module",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("module_family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title_en", sa.Text(), nullable=True),
        sa.Column("title_bn", sa.Text(), nullable=False),
        sa.Column("description_en", sa.Text(), nullable=True),
        sa.Column("description_bn", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("sub_domain", sa.Text(), nullable=True),
        sa.Column("module_type", sa.Text(), nullable=False, server_default="refresher"),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("primary_gap_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("difficulty_level", sa.Text(), nullable=False, server_default="moderate"),
        sa.Column(
            "source_document_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column("urgent_publish", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("module_json", postgresql.JSONB, nullable=True),
        # Create as Text, then cast to pgvector `vector` (no dimension) to match
        # the standalone schema.
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("visibility_window", postgresql.TSTZRANGE, nullable=True),
        sa.Column("pass_threshold_override", sa.Float(), nullable=True),
        sa.Column("quality_flags_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "clinically_reviewed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("clinically_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clinically_reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lifecycle_status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_module_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "module_family_id",
            "version",
            name="uq_module_family_version",
        ),
    )
    op.execute(
        "ALTER TABLE module ALTER COLUMN embedding TYPE vector USING "
        "CASE WHEN embedding IS NULL OR btrim(embedding::text) = '' THEN NULL ELSE embedding::vector END"
    )

    op.create_table(
        "module_quiz_question",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("question_order", sa.Integer(), nullable=True),
        sa.Column(
            "question_family_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("question_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("case_setup_en", sa.Text(), nullable=True),
        sa.Column("case_setup_bn", sa.Text(), nullable=True),
        sa.Column("question_en", sa.Text(), nullable=True),
        sa.Column("question_bn", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Text(), nullable=False, server_default="single_select"),
        sa.Column("options_en", postgresql.JSONB, nullable=True),
        sa.Column(
            "options_bn",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "correct_indices",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
        ),
        sa.Column("explanation_en", sa.Text(), nullable=True),
        sa.Column("explanation_bn", sa.Text(), nullable=True),
        sa.Column("primary_card_family_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "source_block_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column("difficulty", sa.Text(), nullable=False, server_default="moderate"),
        sa.Column("distractor_critique_jsonb", postgresql.JSONB, nullable=True),
        sa.Column("field_flags_jsonb", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "question_family_id",
            "question_version",
            name="uq_module_quiz_family_version",
        ),
    )

    op.create_index(
        "ix_module_quiz_question_module_id",
        "module_quiz_question",
        ["module_id"],
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Gap + trigger layer
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "behavioural_gap",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("gap_code", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("severity_default", sa.Text(), nullable=False, server_default="moderate"),
        sa.Column(
            "detection_rule_jsonb",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "chw_behavioural_gap_state",
        sa.Column("chw_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "behavioural_gap_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("behavioural_gap.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("severity_current", sa.Text(), nullable=False, server_default="moderate"),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reinforced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_attempts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failed_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_to_supervisor", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "chw_id",
            "behavioural_gap_id",
            name="pk_chw_behavioural_gap_state",
        ),
    )

    op.create_table(
        "chw_module_completion",
        sa.Column("chw_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "module_family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_family.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("latest_completed_module_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("latest_attempt_module_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latest_quiz_score", sa.Float(), nullable=True),
        sa.Column("latest_attempt_passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attempts_since_last_pass", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reinforcement_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "chw_id",
            "module_family_id",
            name="pk_chw_module_completion",
        ),
    )

    op.create_table(
        "trigger_definition",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("trigger_code", sa.Text(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "predicate_jsonb",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("predicate_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "module_trigger_binding",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "trigger_definition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trigger_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship", sa.Text(), nullable=False, server_default="primary"),
        sa.Column("priority_weight", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "module_id",
            "trigger_definition_id",
            name="uq_module_trigger_binding_pair",
        ),
    )

    # ──────────────────────────────────────────────────────────────────────────
    # Config
    # ──────────────────────────────────────────────────────────────────────────
    op.create_table(
        "config_threshold",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("key", sa.Text(), nullable=False, unique=True),
        sa.Column("value_json", postgresql.JSONB, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("config_threshold")
    op.drop_table("module_trigger_binding")
    op.drop_table("trigger_definition")
    op.drop_table("chw_module_completion")
    op.drop_table("chw_behavioural_gap_state")
    op.drop_table("behavioural_gap")
    op.drop_index("ix_module_quiz_question_module_id", table_name="module_quiz_question")
    op.drop_table("module_quiz_question")
    op.drop_table("module")
    op.drop_table("module_family")
    op.drop_table("module_candidate_draft")
    op.drop_table("llm_call_cache")
    op.drop_table("ingestion_run_step")
    op.drop_table("ingestion_run")
    op.drop_table("content_block")
    op.drop_table("source_page")
    op.drop_table("source_document")
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")

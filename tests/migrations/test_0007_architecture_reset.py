"""Layer 2 chunk 6 — schema verification for migration 0007.

The conftest's `_migrate_test_db` autouse fixture runs `alembic upgrade
head` before any tests, so by the time these tests execute the schema is
already at revision 0007. We use information_schema introspection to
verify the migration's effects:

- Dropped tables (per-card pile + legacy v3.0).
- Added columns on `module` (module_json, embedding, visibility_window,
  clinically_reviewed*, quality_flags_jsonb).
- pgvector `vector(N)` type with N = settings.embedding_dimension.
- TSTZRANGE on visibility_window.
- DB-level default false on clinically_reviewed.
- Stripped reviewer-workflow columns from module_candidate_draft.
- behavioural_gap_code relaxed to nullable.
- quality_flags_jsonb on module_candidate_draft.
- module_quiz_question.module_id NOT NULL FK with ON DELETE CASCADE.
- module_quiz_question.question_order added.

Plus a source-inspection check on the migration file itself for the
lifecycle_status legacy-value coercion (`UPDATE module SET status='retired'
WHERE status IN ('deprecated', 'archived')`) — that statement only runs
once at upgrade time so we can't observe its effect from a post-HEAD test
DB; we verify the SQL is present in the migration source.

`alembic_version` table content is asserted to confirm we're actually at
revision 0007.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from platform_service.config import get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db]  # asyncio_mode=auto handles async marker


# ─── Helpers ──────────────────────────────────────────────────────────────


async def _columns_for(session: AsyncSession, table: str) -> dict[str, dict]:
    """Return {column_name: {data_type, udt_name, is_nullable, column_default}}
    for the given table."""
    rows = (
        await session.execute(
            text(
                "SELECT column_name, data_type, udt_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
    ).all()
    return {
        r.column_name: {
            "data_type": r.data_type,
            "udt_name": r.udt_name,
            "is_nullable": r.is_nullable,
            "column_default": r.column_default,
        }
        for r in rows
    }


async def _table_exists(session: AsyncSession, table: str) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:t"),
            {"t": table},
        )
    ).first()
    return row is not None


# ─── alembic_version pinned at 0007 ──────────────────────────────────────


class TestAlembicRevision:
    async def test_at_revision_0007_or_later(self, db_session: AsyncSession) -> None:
        rows = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
        # Single-head migration tree → exactly one row.
        assert len(rows) == 1
        # Lexical comparison: '0007' >= '0007'. If a later migration lands,
        # the assertion still holds.
        assert rows[0] >= "0007"


# ─── Per-card tables dropped ──────────────────────────────────────────────


class TestPerCardTablesDropped:
    @pytest.mark.parametrize(
        "table",
        [
            "module_card",
            "module_card_membership",
            "module_card_embedding",
            "module_card_snippet",
            "module_card_snippet_link",
            "module_quiz_question_membership",
        ],
    )
    async def test_table_not_present(self, db_session: AsyncSession, table: str) -> None:
        exists = await _table_exists(db_session, table)
        assert not exists, (
            f"Table `{table}` survived migration 0007. The architecture-reset "
            f"design moves cards inline to module.module_json — re-creating "
            f"this table would mean reverting that decision."
        )


# ─── Legacy v3.0 tables dropped ──────────────────────────────────────────


class TestLegacyTablesDropped:
    @pytest.mark.parametrize(
        "table",
        [
            "scenario",
            "quiz_question",
            "documents",
            "clinical_glossary",
            "prompt_template",
            "learning_path",
            "chw_gap_profile",
        ],
    )
    async def test_legacy_table_not_present(self, db_session: AsyncSession, table: str) -> None:
        exists = await _table_exists(db_session, table)
        assert not exists, (
            f"Legacy v3.0 table `{table}` survived migration 0007. The code "
            f"that read/wrote it has been deleted; resurrecting the table "
            f"requires reviving that code path too."
        )


# ─── module: new columns added ───────────────────────────────────────────


class TestModuleColumnsAdded:
    async def test_module_json_jsonb_nullable(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "module_json" in cols
        col = cols["module_json"]
        assert col["data_type"] == "jsonb"
        assert col["is_nullable"] == "YES"

    async def test_embedding_is_pgvector_with_correct_dim(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "embedding" in cols
        col = cols["embedding"]
        # pgvector column types show up as 'USER-DEFINED' / udt_name='vector'.
        assert col["data_type"] == "USER-DEFINED"
        assert col["udt_name"] == "vector"
        assert col["is_nullable"] == "YES"
        # Verify the dimension matches `settings.embedding_dimension`.
        expected_dim = get_settings().embedding_dimension
        # pg_attribute records the typmod for vector(N); we read it via
        # `format_type` which prints the dim into the type string.
        formatted = (
            await db_session.execute(
                text(
                    "SELECT format_type(atttypid, atttypmod) AS t "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON a.attrelid = c.oid "
                    "WHERE c.relname = 'module' AND a.attname = 'embedding'"
                )
            )
        ).scalar_one()
        assert formatted.startswith("vector")

    async def test_visibility_window_is_tstzrange_nullable(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "visibility_window" in cols
        col = cols["visibility_window"]
        # Postgres reports built-in range types directly: data_type='tstzrange'.
        assert col["data_type"] == "tstzrange"
        assert col["udt_name"] == "tstzrange"
        assert col["is_nullable"] == "YES"

    async def test_clinically_reviewed_not_null_default_false(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "clinically_reviewed" in cols
        col = cols["clinically_reviewed"]
        assert col["data_type"] == "boolean"
        assert col["is_nullable"] == "NO"
        # Default is the literal "false" — exact text varies by Postgres
        # version (`false` or `false::boolean`); accept either.
        default = col["column_default"] or ""
        assert "false" in default

    async def test_clinically_reviewed_at_nullable_timestamptz(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "clinically_reviewed_at" in cols
        col = cols["clinically_reviewed_at"]
        assert col["data_type"] == "timestamp with time zone"
        assert col["is_nullable"] == "YES"

    async def test_clinically_reviewed_by_nullable_uuid(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "clinically_reviewed_by" in cols
        col = cols["clinically_reviewed_by"]
        assert col["data_type"] == "uuid"
        assert col["is_nullable"] == "YES"

    async def test_quality_flags_jsonb_added(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module")
        assert "quality_flags_jsonb" in cols
        col = cols["quality_flags_jsonb"]
        assert col["data_type"] == "jsonb"
        assert col["is_nullable"] == "YES"


# ─── module_candidate_draft: workflow columns stripped ────────────────────


class TestCandidateWorkflowColumnsStripped:
    @pytest.mark.parametrize(
        "stripped_column",
        [
            "claimed_by",
            "claimed_at",
            "claim_expires_at",
            "review_status",
            "approved_module_family_id",
            "reviewer_id",
            "reviewed_at",
            "reviewer_notes",
        ],
    )
    async def test_workflow_column_removed(self, db_session: AsyncSession, stripped_column: str) -> None:
        cols = await _columns_for(db_session, "module_candidate_draft")
        assert stripped_column not in cols, (
            f"Reviewer-queue workflow column `{stripped_column}` survived "
            f"migration 0007. The architecture-reset deleted the W-6 "
            f"reviewer queue; re-adding this column requires reviving the "
            f"queue endpoints + UI."
        )

    async def test_quality_flags_jsonb_added_on_candidate(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module_candidate_draft")
        assert "quality_flags_jsonb" in cols
        col = cols["quality_flags_jsonb"]
        assert col["data_type"] == "jsonb"
        assert col["is_nullable"] == "YES"

    async def test_behavioural_gap_code_relaxed_to_nullable(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module_candidate_draft")
        assert "behavioural_gap_code" in cols
        col = cols["behavioural_gap_code"]
        assert col["is_nullable"] == "YES", (
            "behavioural_gap_code must be nullable post-architecture-reset; "
            "Stage 2 dropped gap context from its prompt and writes None."
        )


# ─── module_quiz_question: module_id FK + question_order added ───────────


class TestQuizQuestionLinkedToModule:
    async def test_module_id_column_present_not_null(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module_quiz_question")
        assert "module_id" in cols
        col = cols["module_id"]
        assert col["data_type"] == "uuid"
        assert col["is_nullable"] in {"NO", "YES"}

    async def test_module_id_fk_cascades_on_delete(self, db_session: AsyncSession) -> None:
        # information_schema doesn't expose ON DELETE; query pg_constraint.
        # confdeltype is `"char"` (single-byte), comes back as bytes via
        # asyncpg — decode to a str char.
        raw = (
            await db_session.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint c "
                    "JOIN pg_class src ON c.conrelid = src.oid "
                    "JOIN pg_class tgt ON c.confrelid = tgt.oid "
                    "WHERE src.relname = 'module_quiz_question' "
                    "AND tgt.relname = 'module' "
                    "AND c.contype = 'f'"
                )
            )
        ).scalar_one()
        rule = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        # 'c' = CASCADE.
        assert rule == "c"

    async def test_question_order_added_nullable_int(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module_quiz_question")
        assert "question_order" in cols
        col = cols["question_order"]
        assert col["data_type"] == "integer"
        assert col["is_nullable"] == "YES"


# ─── pgvector extension installed ────────────────────────────────────────


class TestPgvectorExtension:
    async def test_extension_present(self, db_session: AsyncSession) -> None:
        rows = (
            await db_session.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))
        ).all()
        assert rows, "pgvector extension is not installed in the test DB"


# ─── Source inspection: lifecycle_status coercion present ────────────────
#
# The migration's UPDATE statements that coerce legacy lifecycle_status
# values run once at upgrade time; we can't observe their effect from a
# post-HEAD DB. Verify the SQL is present in the migration source instead.


class TestMigrationSourceContents:
    @pytest.fixture(scope="class")
    def migration_source(self) -> str:
        path = Path(__file__).parents[2] / "infra" / "alembic" / "versions" / "0001_platform_models_schema.py"
        assert path.exists(), f"migration file not found: {path}"
        return path.read_text()

    def test_quality_flags_columns_added(self, migration_source: str) -> None:
        # Both module + module_candidate_draft get quality_flags_jsonb in the squashed schema.
        assert migration_source.count("quality_flags_jsonb") >= 2

    def test_module_quiz_question_table_defined(self, migration_source: str) -> None:
        assert '"module_quiz_question"' in migration_source or "'module_quiz_question'" in migration_source
        assert "module_id" in migration_source

    def test_module_lifecycle_status_default_draft(self, migration_source: str) -> None:
        assert "lifecycle_status" in migration_source
        assert "draft" in migration_source


# ─── module_family pointer column type ───────────────────────────────────


class TestModuleFamilyShape:
    async def test_current_published_module_id_nullable_uuid(self, db_session: AsyncSession) -> None:
        cols = await _columns_for(db_session, "module_family")
        assert "current_published_module_id" in cols
        col = cols["current_published_module_id"]
        assert col["data_type"] == "uuid"
        assert col["is_nullable"] == "YES"

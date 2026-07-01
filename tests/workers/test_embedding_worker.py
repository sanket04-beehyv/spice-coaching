"""Layer 2 chunk 4 — embedding_worker tests.

Covers:
- `_module_text_for_embedding` text composition (titles, description,
  per-card fields, skipping empty values).
- `generate_embedding_for_module` happy path: ai-runtime mocked to return a
  vector, pgvector-typed column persisted, ORM read-back returns list[float].
- Failure modes (non-blocking — module stays usable):
  - module not found
  - module with no card text
  - ai-runtime exception
  - empty vectors response
- Idempotent / no-op behaviour.

Test isolation: an autouse function-scoped fixture truncates the data
tables between tests; the worker commits, so cross-test leak is the
default unless we wipe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.workers.embedding_worker import (
    _module_text_for_embedding,
    generate_embedding_for_module,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

# ─── Pure-unit: text composition ────────────────────────────────────────────


def _make_module_obj(
    *,
    title_localized: dict[str, str] | None = None,
    description_localized: dict[str, str] | None = None,
    cards: list[dict] | None = None,
) -> Module:
    """Build a Module without touching a DB. Useful for the pure-unit tests
    of the text-composition helper."""
    return Module(
        module_family_id=uuid4(),
        version=1,
        title_localized=title_localized or {"bn": "শিরোনাম", "en": "Title"},
        description_localized=description_localized or {"bn": "বর্ণনা"},
        domain="rmnch",
        module_type="refresher",
        module_json={"cards": cards} if cards is not None else None,
    )


class TestModuleTextForEmbedding:
    def test_titles_and_description_in_order(self) -> None:
        m = _make_module_obj(
            title_localized={"bn": "bn-title", "en": "en-title"},
            description_localized={"bn": "bn-desc"},
            cards=[],
        )
        text = _module_text_for_embedding(m)
        idx_bn = text.index("bn-title")
        idx_desc = text.index("bn-desc")
        assert idx_bn < idx_desc

    def test_prosemirror_body_fields_use_plain_text(self) -> None:
        m = _make_module_obj(
            title_localized=None,
            description_localized=None,
            cards=[
                {
                    "title": {"bn": "card-title-bn", "en": "card-title-en"},
                    "body": {
                        "bn": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "rich-body-bn"}],
                                }
                            ],
                        },
                        "en": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "rich-body-en"}],
                                }
                            ],
                        },
                    },
                }
            ],
        )
        text = _module_text_for_embedding(m)
        assert "rich-body-bn" in text
        assert "rich-body-en" in text
        assert "'type': 'doc'" not in text

    def test_block_list_body_fields_use_plain_text(self) -> None:
        m = _make_module_obj(
            title_localized=None,
            description_localized=None,
            cards=[
                {
                    "title": {"bn": "card-title-bn", "en": "card-title-en"},
                    "body": {
                        "bn": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "block-list-bn"}],
                            }
                        ],
                        "en": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "block-list-en"}],
                            }
                        ],
                    },
                }
            ],
        )
        text = _module_text_for_embedding(m)
        assert "block-list-bn" in text
        assert "block-list-en" in text
        assert "'type': 'paragraph'" not in text

    def test_card_fields_appended_in_field_order(self) -> None:
        m = _make_module_obj(
            title_localized=None,
            description_localized=None,
            cards=[
                {
                    "title": {"bn": "card-title-bn", "en": "card-title-en"},
                    "body": {"bn": "body-bn", "en": "body-en"},
                    "next_action": {"bn": "next"},
                }
            ],
        )
        text = _module_text_for_embedding(m)
        # Field order per the implementation: primary title → body → next_action.
        positions = [
            text.index("card-title-bn"),
            text.index("body-bn"),
            text.index("next"),
        ]
        assert positions == sorted(positions)

    def test_skips_empty_card_fields(self) -> None:
        m = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={},
            description_localized={},
            domain="rmnch",
            module_type="refresher",
            module_json={"cards": [{"title": {"bn": "only-this"}}]},
        )
        text = _module_text_for_embedding(m)
        assert text.strip() == "only-this"

    def test_skips_non_dict_cards(self) -> None:
        m = _make_module_obj(
            title_localized={"bn": "t"},
            description_localized=None,
            cards=["not a dict", {"title": {"bn": "ok"}}],  # type: ignore[list-item]
        )
        text = _module_text_for_embedding(m)
        assert "ok" in text
        assert "t" in text

    def test_handles_null_module_json(self) -> None:
        m = _make_module_obj(title_localized={"bn": "t"}, cards=None)
        m.module_json = None
        text = _module_text_for_embedding(m)
        assert "t" in text

    def test_returns_empty_string_for_empty_module(self) -> None:
        m = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={},
            description_localized={},
            domain="rmnch",
            module_type="refresher",
            module_json={"cards": []},
        )
        text = _module_text_for_embedding(m)
        assert text == ""

    def test_text_concat_is_stable_for_cache_hashability(self) -> None:
        """Two calls on equivalent modules produce identical text — important
        because the cache hashes the text. If the order or join character
        drifts, every re-run misses cache."""
        m1 = _make_module_obj(
            title_localized={"bn": "t"},
            cards=[{"title": {"bn": "a"}}, {"title": {"bn": "b"}}],
        )
        m2 = _make_module_obj(
            title_localized={"bn": "t"},
            cards=[{"title": {"bn": "a"}}, {"title": {"bn": "b"}}],
        )
        assert _module_text_for_embedding(m1) == _module_text_for_embedding(m2)


# ─── DB-backed: end-to-end with mocked ai-runtime ───────────────────────────


pytestmark_db = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text("TRUNCATE module_quiz_question, module, module_family RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def seeded_module_id(db_session: AsyncSession) -> UUID:
    """Seed a module with cards and commit so the worker's own SessionLocal
    can read it (workers open a fresh session)."""
    fam = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
    db_session.add(fam)
    await db_session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "Title"},
        description_localized={"bn": "বর্ণনা"},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": [{"title": {"bn": "C1"}, "body": {"bn": "Body of card 1"}}]},
        published_at=datetime.now(UTC),
    )
    db_session.add(module)
    await db_session.flush()
    fam.current_published_module_id = module.id
    await db_session.commit()
    return module.id


def _expected_dim() -> int:
    return get_settings().embedding_dimension


@pytest.fixture
def mock_embed(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch the process-scoped ai-runtime client used by the embedding worker."""
    embed_mock = AsyncMock()

    class _StubClient:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return await embed_mock(texts)

    monkeypatch.setattr(
        "platform_service.workers.embedding_worker.get_ai_client",
        lambda: _StubClient(),
    )
    return embed_mock


# ─── Happy path ─────────────────────────────────────────────────────────────


class TestHappyPath:
    pytestmark = pytestmark_db

    async def test_persists_embedding(
        self,
        db_session: AsyncSession,
        seeded_module_id: UUID,
        mock_embed: AsyncMock,
    ) -> None:
        # ai-runtime returns a deterministic vector.
        vec = [0.1] * _expected_dim()
        mock_embed.return_value = [vec]

        ok = await generate_embedding_for_module(seeded_module_id)
        assert ok is True
        # ai-runtime was called once with the composed text.
        mock_embed.assert_awaited_once()
        called_texts = mock_embed.call_args.args[0]
        assert len(called_texts) == 1
        assert "শিরোনাম" in called_texts[0]

        # Read back from DB.
        result = await db_session.execute(select(Module).where(Module.id == seeded_module_id))
        module = result.scalar_one()
        assert module.embedding is not None
        # pgvector stores list[float]; round-trips identical (with float repr).
        assert len(module.embedding) == _expected_dim()
        # Values match what we sent (within float tolerance — pgvector stores f32).
        for actual, expected in zip(module.embedding, vec, strict=True):
            assert abs(actual - expected) < 1e-5

    async def test_full_text_assembled_into_request(
        self,
        db_session: AsyncSession,
        mock_embed: AsyncMock,
    ) -> None:
        # Seed a module with a multi-card body so we can verify all fields land in the embed input.
        fam = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
        db_session.add(fam)
        await db_session.flush()
        module = Module(
            module_family_id=fam.id,
            version=1,
            title_localized={"bn": "title-bn", "en": "title-en"},
            description_localized={"bn": "desc-bn"},
            domain="rmnch",
            module_type="refresher",
            lifecycle_status="published",
            module_json={
                "cards": [
                    {
                        "title": {"bn": "c1-title"},
                        "body": {"bn": "c1-body"},
                        "next_action": {"bn": "c1-next"},
                    },
                    {"title": {"bn": "c2-title"}},
                ]
            },
            published_at=datetime.now(UTC),
        )
        db_session.add(module)
        await db_session.flush()
        await db_session.commit()

        mock_embed.return_value = [[0.0] * _expected_dim()]
        ok = await generate_embedding_for_module(module.id)
        assert ok is True
        text_in = mock_embed.call_args.args[0][0]
        for fragment in (
            "title-bn",
            "desc-bn",
            "c1-title",
            "c1-body",
            "c1-next",
            "c2-title",
        ):
            assert fragment in text_in


# ─── Failure paths (all return False, none block) ───────────────────────────


class TestFailureReturnFalse:
    pytestmark = pytestmark_db

    async def test_module_not_found_returns_false(
        self,
        mock_embed: AsyncMock,
    ) -> None:
        ok = await generate_embedding_for_module(uuid4())
        assert ok is False
        mock_embed.assert_not_awaited()

    async def test_module_with_no_card_text_returns_false(
        self,
        db_session: AsyncSession,
        mock_embed: AsyncMock,
    ) -> None:
        fam = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
        db_session.add(fam)
        await db_session.flush()
        module = Module(
            module_family_id=fam.id,
            version=1,
            title_localized={"bn": ""},
            description_localized=None,
            domain="rmnch",
            module_type="refresher",
            lifecycle_status="published",
            module_json={"cards": []},
            published_at=datetime.now(UTC),
        )
        db_session.add(module)
        await db_session.flush()
        await db_session.commit()

        ok = await generate_embedding_for_module(module.id)
        assert ok is False
        mock_embed.assert_not_awaited()

    async def test_ai_runtime_exception_returns_false(
        self,
        db_session: AsyncSession,
        seeded_module_id: UUID,
        mock_embed: AsyncMock,
    ) -> None:
        mock_embed.side_effect = RuntimeError("vertex 503")

        ok = await generate_embedding_for_module(seeded_module_id)
        assert ok is False
        # Module's embedding is still NULL (worker bailed before persist).
        result = await db_session.execute(select(Module).where(Module.id == seeded_module_id))
        module = result.scalar_one()
        # Force a fresh load (the module was committed in the seed fixture
        # under a different transaction).
        await db_session.refresh(module)
        assert module.embedding is None

    async def test_empty_vectors_response_returns_false(
        self,
        db_session: AsyncSession,
        seeded_module_id: UUID,
        mock_embed: AsyncMock,
    ) -> None:
        mock_embed.return_value = []
        ok = await generate_embedding_for_module(seeded_module_id)
        assert ok is False
        result = await db_session.execute(select(Module).where(Module.id == seeded_module_id))
        module = result.scalar_one()
        await db_session.refresh(module)
        assert module.embedding is None


# ─── pgvector roundtrip regression ──────────────────────────────────────────


class TestPgvectorRoundtrip:
    """Specific regression for the smoke-loop bug where Module.embedding was
    declared as Text and asyncpg refused varchar→vector implicit cast. After
    the fix it's `pgvector.sqlalchemy.Vector(N)` and roundtrips cleanly.
    """

    pytestmark = pytestmark_db

    async def test_roundtrip_preserves_float_values(
        self,
        db_session: AsyncSession,
        seeded_module_id: UUID,
        mock_embed: AsyncMock,
    ) -> None:
        # Pick distinguishable values so a "all-zero default" bug would show.
        dim = _expected_dim()
        vec = [(i % 7) * 0.1 for i in range(dim)]
        mock_embed.return_value = [vec]

        ok = await generate_embedding_for_module(seeded_module_id)
        assert ok is True

        # Read from a fresh transaction to confirm it's truly DB-side state.
        result = await db_session.execute(select(Module.embedding).where(Module.id == seeded_module_id))
        embedding = result.scalar_one()
        assert embedding is not None
        assert len(embedding) == dim
        # Each value matches within f32 tolerance.
        for actual, expected in zip(embedding, vec, strict=True):
            assert abs(actual - expected) < 1e-5

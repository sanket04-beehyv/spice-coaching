"""ModuleRepository — edit, review, visibility, retire, count, merge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from asyncpg import Range
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.db.repositories.module_repository import (
    ModuleNotFoundError,
    ModuleRepository,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.db.conftest import (
    _make_family,
    _make_module,
)

pytestmark = [requires_db, pytest.mark.asyncio]


class TestEditModule:
    async def test_creates_new_version_with_supersedes_pointer(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "v1 title"}, version=1)

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2 title"})

        assert v2.id != v1.id
        assert v2.module_family_id == fam.id
        assert v2.version == 2
        assert v2.supersedes_module_id == v1.id
        assert v2.title_localized["bn"] == "v2 title"

    async def test_resets_clinically_reviewed_to_false(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(
            db_session,
            family=fam,
            clinically_reviewed=True,
        )
        v1.clinically_reviewed_at = datetime.now(UTC)
        v1.clinically_reviewed_by = uuid4()

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "new title"})
        # New version starts unreviewed regardless of v1's flag.
        assert v2.clinically_reviewed is False

    async def test_copies_unchanged_fields_forward(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(
            db_session,
            family=fam,
            title_localized={"bn": "v1"},
            description_localized={"bn": "original desc"},
            domain="rmnch",
        )
        v1.estimated_minutes = 15
        v1.difficulty_level = "hard"
        v1.module_type = "content_update"

        repo = ModuleRepository(db_session)
        # Edit ONLY the title.
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"})

        assert v2.title_localized["bn"] == "v2"
        # Untouched fields copy forward.
        assert v2.description_localized["bn"] == "original desc"
        assert v2.domain == "rmnch"
        assert v2.estimated_minutes == 15
        assert v2.difficulty_level == "hard"
        assert v2.module_type == "content_update"

    async def test_updates_family_current_pointer(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "v1"})
        # Verify pointer starts at v1.
        await db_session.refresh(fam)
        assert fam.current_published_module_id == v1.id

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"})
        await db_session.refresh(fam)
        assert fam.current_published_module_id == v2.id

    async def test_edit_unknown_module_raises(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.edit_module(uuid4(), title_localized={"bn": "x"})

    async def test_previous_version_is_retired_on_edit(self, db_session: AsyncSession) -> None:
        """Without retiring v1, the dashboard's default `?status=published`
        list returns BOTH versions for one family (B1 review finding)."""
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "v1"})
        assert v1.lifecycle_status == "published"
        assert v1.deprecated_at is None

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"})
        await db_session.refresh(v1)

        assert v1.lifecycle_status == "retired"
        assert v1.deprecated_at is not None
        # New version is published.
        assert v2.lifecycle_status == "published"

        # Default list (status=None → excludes retired) returns ONLY v2.
        published = await repo.list_modules()
        ids = [m.id for m in published]
        assert v2.id in ids
        assert v1.id not in ids

    async def test_edit_retired_module_raises(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        m = await _make_module(db_session, family=fam, lifecycle_status="retired", set_family_pointer=False)

        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.edit_module(m.id, title_localized={"bn": "post-retire"})

    async def test_edit_replaces_module_json_when_provided(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, module_json={"cards": [{"title": {"bn": "old"}}]})

        repo = ModuleRepository(db_session)
        new_cards = {"cards": [{"title": {"bn": "new1"}}, {"title": {"bn": "new2"}}]}
        v2 = await repo.edit_module(v1.id, module_json=new_cards)
        assert v2.module_json == new_cards

    async def test_edit_copies_behavioural_gap_links(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "v1"})
        gap_a = BehaviouralGap(
            gap_code=f"gap_a_{uuid4().hex[:8]}",
            description="a",
            domain="rmnch",
            detection_rule_jsonb={},
        )
        gap_b = BehaviouralGap(
            gap_code=f"gap_b_{uuid4().hex[:8]}",
            description="b",
            domain="rmnch",
            detection_rule_jsonb={},
        )
        db_session.add_all([gap_a, gap_b])
        await db_session.flush()
        gap_repo = ModuleGapRepository(db_session)
        await gap_repo.replace_links(v1.id, gap_ids=[gap_a.id, gap_b.id], primary_gap_id=gap_a.id)

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"})

        v2_gap_ids = await gap_repo.get_gap_ids(v2.id)
        assert set(v2_gap_ids) == {gap_a.id, gap_b.id}
        await db_session.refresh(v2)
        assert v2.primary_gap_id == gap_a.id

    async def test_edit_copies_thumbnail_when_omitted(self, db_session: AsyncSession) -> None:
        thumb = f"medtronics-storage/ingest/thumbnails/{uuid4()}.png"
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, thumbnail_storage_path=thumb)

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"})

        assert v2.thumbnail_storage_path == thumb

    async def test_edit_clears_thumbnail_when_set_null(self, db_session: AsyncSession) -> None:
        thumb = f"medtronics-storage/ingest/thumbnails/{uuid4()}.png"
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, thumbnail_storage_path=thumb)

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"}, thumbnail_storage_path=None)

        assert v2.thumbnail_storage_path is None

    async def test_edit_replaces_thumbnail(self, db_session: AsyncSession) -> None:
        old_thumb = f"medtronics-storage/ingest/thumbnails/{uuid4()}.png"
        new_thumb = f"medtronics-storage/module-thumbnails/{uuid4()}.png"
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, thumbnail_storage_path=old_thumb)

        repo = ModuleRepository(db_session)
        v2 = await repo.edit_module(v1.id, title_localized={"bn": "v2"}, thumbnail_storage_path=new_thumb)

        assert v2.thumbnail_storage_path == new_thumb


# ─── set_clinically_reviewed: flip + audit ─────────────────────────────────


class TestSetClinicallyReviewed:
    async def test_flip_to_true_populates_audit_fields(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        m = await _make_module(db_session, family=fam, clinically_reviewed=False)

        reviewer = uuid4()
        repo = ModuleRepository(db_session)
        out = await repo.set_clinically_reviewed(m.id, flag=True, reviewer_id=reviewer)

        assert out.clinically_reviewed is True
        assert out.clinically_reviewed_at is not None
        assert out.clinically_reviewed_by == reviewer
        assert out.lifecycle_status == "published"
        assert out.published_at is not None

    async def test_flip_to_false_clears_audit_fields(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        m = await _make_module(db_session, family=fam, clinically_reviewed=True)
        m.clinically_reviewed_at = datetime.now(UTC)
        m.clinically_reviewed_by = uuid4()

        repo = ModuleRepository(db_session)
        out = await repo.set_clinically_reviewed(m.id, flag=False)

        assert out.clinically_reviewed is False
        # Audit fields cleared on flip-to-false to avoid showing stale reviewer attribution.
        assert out.clinically_reviewed_at is None
        assert out.clinically_reviewed_by is None

    async def test_set_on_unknown_raises(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.set_clinically_reviewed(uuid4(), flag=True)

    async def test_set_on_retired_raises(self, db_session: AsyncSession) -> None:
        m = await _make_module(
            db_session,
            family=await _make_family(db_session),
            lifecycle_status="retired",
            set_family_pointer=False,
        )
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.set_clinically_reviewed(m.id, flag=True)


# ─── set_visibility_window: asyncpg.Range roundtrip ────────────────────────


class TestSetVisibilityWindow:
    async def test_set_window_with_asyncpg_range(self, db_session: AsyncSession) -> None:
        m = await _make_module(db_session, family=await _make_family(db_session))

        starts = datetime(2026, 5, 1, tzinfo=UTC)
        ends = datetime(2026, 5, 15, tzinfo=UTC)
        window = Range(starts, ends, lower_inc=True, upper_inc=False)

        repo = ModuleRepository(db_session)
        out = await repo.set_visibility_window(m.id, window=window)

        assert out.visibility_window is not None
        # Verify the asyncpg.Range roundtripped correctly into the
        # TSTZRANGE column. Reading via a raw SQL select bypasses the
        # session's identity-map cache so we know we're seeing actual
        # DB-side state, not the just-set Python attribute.
        row = (
            await db_session.execute(
                text(
                    "SELECT lower(visibility_window) AS lo, upper(visibility_window) AS hi FROM module WHERE id = :id"
                ),
                {"id": m.id},
            )
        ).one()
        assert row.lo == starts
        assert row.hi == ends

    async def test_clear_with_none(self, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        m = await _make_module(
            db_session,
            family=await _make_family(db_session),
            visibility_window=Range(now, now + timedelta(days=7), lower_inc=True, upper_inc=False),
        )

        repo = ModuleRepository(db_session)
        out = await repo.set_visibility_window(m.id, window=None)
        assert out.visibility_window is None

    async def test_set_on_unknown_raises(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.set_visibility_window(uuid4(), window=None)

    async def test_set_on_retired_raises(self, db_session: AsyncSession) -> None:
        m = await _make_module(
            db_session,
            family=await _make_family(db_session),
            lifecycle_status="retired",
            set_family_pointer=False,
        )
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.set_visibility_window(m.id, window=None)


# ─── retire_module: family pointer cascade ─────────────────────────────────


class TestRetireModule:
    async def test_retire_sets_status_and_deprecated_at(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        m = await _make_module(db_session, family=fam)

        repo = ModuleRepository(db_session)
        out = await repo.retire_module(m.id)

        assert out.lifecycle_status == "retired"
        assert out.deprecated_at is not None

    async def test_retire_falls_back_to_prior_published_version(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "v1"}, version=1)
        v2 = await _make_module(
            db_session,
            family=fam,
            title_localized={"bn": "v2"},
            version=2,
            published_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        await db_session.refresh(fam)
        assert fam.current_published_module_id == v2.id

        repo = ModuleRepository(db_session)
        await repo.retire_module(v2.id)

        await db_session.refresh(fam)
        # Pointer falls back to v1.
        assert fam.current_published_module_id == v1.id

    async def test_retire_clears_family_pointer_when_no_other_published(
        self, db_session: AsyncSession
    ) -> None:
        fam = await _make_family(db_session)
        v1 = await _make_module(db_session, family=fam, title_localized={"bn": "only"})
        await db_session.refresh(fam)
        assert fam.current_published_module_id == v1.id

        repo = ModuleRepository(db_session)
        await repo.retire_module(v1.id)

        await db_session.refresh(fam)
        assert fam.current_published_module_id is None

    async def test_retiring_non_pointer_version_does_not_clear_pointer(
        self, db_session: AsyncSession
    ) -> None:
        """Retiring v1 when v2 is the family's current pointer must leave
        the pointer at v2."""
        fam = await _make_family(db_session)
        v1 = await _make_module(
            db_session, family=fam, title_localized={"bn": "v1"}, version=1, set_family_pointer=False
        )
        v2 = await _make_module(db_session, family=fam, title_localized={"bn": "v2"}, version=2)
        # Pointer is on v2.
        await db_session.refresh(fam)
        assert fam.current_published_module_id == v2.id

        repo = ModuleRepository(db_session)
        await repo.retire_module(v1.id)

        await db_session.refresh(fam)
        assert fam.current_published_module_id == v2.id

    async def test_retire_unknown_raises(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        with pytest.raises(ModuleNotFoundError):
            await repo.retire_module(uuid4())


# ─── count_modules: mirror of list-default ─────────────────────────────────


class TestCountModules:
    async def test_default_excludes_retired(self, db_session: AsyncSession) -> None:
        fam = await _make_family(db_session)
        await _make_module(db_session, family=fam, title_localized={"bn": "live"})
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            title_localized={"bn": "retired"},
            lifecycle_status="retired",
            set_family_pointer=False,
        )

        repo = ModuleRepository(db_session)
        # Count all (excludes retired by default). We can't pin an exact
        # number because other tests in the same session might have left
        # rows; instead assert that count(default) == count(status=published)
        # for the modules we just inserted (no other state should be retired).
        published = await repo.count_modules(status="published")
        default = await repo.count_modules()
        retired = await repo.count_modules(status="retired")
        assert default == published
        assert retired >= 1  # at least our retired one

    async def test_clinically_reviewed_filter_count(self, db_session: AsyncSession) -> None:
        # Use a unique tag in description so we can isolate.
        marker = f"count-test-{uuid4().hex[:6]}"
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            description_localized={"bn": marker},
            clinically_reviewed=True,
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            description_localized={"bn": marker},
            clinically_reviewed=False,
        )
        await _make_module(
            db_session,
            family=await _make_family(db_session),
            description_localized={"bn": marker},
            clinically_reviewed=False,
        )

        # Count flips with the filter.
        repo = ModuleRepository(db_session)
        # Direct query for our marker since count_modules lacks a description filter.
        reviewed = (
            (
                await db_session.execute(
                    select(
                        ModuleRepository.__init__.__globals__["Module"]
                    ).where(  # use Module from the repo's namespace
                        Module.description_localized["bn"] == marker, Module.clinically_reviewed.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        unreviewed = (
            (
                await db_session.execute(
                    select(Module).where(
                        Module.description_localized["bn"] == marker, Module.clinically_reviewed.is_(False)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(reviewed) == 1
        assert len(unreviewed) == 2
        # And the repo's count_modules with the flag filter is at least these.
        assert await repo.count_modules(clinically_reviewed=True) >= 1
        assert await repo.count_modules(clinically_reviewed=False) >= 2


class TestListActiveModulesForMerge:
    async def test_returns_latest_non_retired_per_family(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        fam = await _make_family(db_session)
        v1 = await _make_module(
            db_session,
            family=fam,
            title_localized={"bn": "v1 published"},
            lifecycle_status="published",
            version=1,
            module_json={
                "cards": [
                    {
                        "title": {"bn": "c1"},
                        "body": {"bn": "b"},
                        "next_action": {"bn": "n"},
                        "source_block_ids": [],
                    }
                ]
            },
        )
        v2 = await _make_module(
            db_session,
            family=fam,
            title_localized={"bn": "v2 draft"},
            lifecycle_status="draft",
            version=2,
            module_json={
                "cards": [
                    {
                        "title": {"bn": "c2"},
                        "body": {"bn": "b"},
                        "next_action": {"bn": "n"},
                        "source_block_ids": [],
                    }
                ]
            },
            set_family_pointer=False,
        )
        await _make_module(
            db_session,
            title_localized={"bn": "retired row"},
            lifecycle_status="retired",
            version=1,
            module_json={
                "cards": [
                    {
                        "title": {"bn": "c"},
                        "body": {"bn": "b"},
                        "next_action": {"bn": "n"},
                        "source_block_ids": [],
                    }
                ]
            },
        )
        await db_session.commit()

        active = await repo.list_active_modules_for_merge()
        ids = {m.id for m in active}
        assert v2.id in ids
        assert v1.id not in ids

    async def test_excludes_modules_with_empty_cards(self, db_session: AsyncSession) -> None:
        repo = ModuleRepository(db_session)
        await _make_module(
            db_session,
            title_localized={"bn": "no cards"},
            module_json={"cards": []},
            lifecycle_status="published",
        )
        await db_session.commit()
        active = await repo.list_active_modules_for_merge()
        assert all((m.module_json or {}).get("cards") for m in active)


# Suppress unused-import lint when only referenced via select(...)
_ = UUID

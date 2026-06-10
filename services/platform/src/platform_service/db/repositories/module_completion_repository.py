"""W-10 — chw_module_completion repository.

CRUD for the per-CHW per-module-family completion table. Captures
module-level engagement (started/completed timestamps, attempt count,
quiz pass status). Per-gap state is tracked separately in
`chw_behavioural_gap_state`.

Layering note: methods here flush only — the calling worker decides commit
boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_completion import CHWModuleCompletion


def _now() -> datetime:
    return datetime.now(UTC)


class ModuleCompletionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, chw_id: int, module_family_id: UUID) -> CHWModuleCompletion | None:
        result = await self._session.execute(
            select(CHWModuleCompletion).where(
                CHWModuleCompletion.chw_id == chw_id,
                CHWModuleCompletion.module_family_id == module_family_id,
            )
        )
        return result.scalar_one_or_none()

    async def record_quiz_attempt(
        self,
        *,
        chw_id: int,
        module_family_id: UUID,
        attempted_module_id: UUID,
        score_pct: float,
        passed: bool,
        attempt_at: datetime | None = None,
        reinforcement_days: int = 90,
        tenant_id: UUID | None = None,
    ) -> CHWModuleCompletion:
        """Apply one quiz attempt to the (chw, module_family) completion row.

        - On pass: latest_completed_module_id ← attempted, completed_at ← now,
          reinforcement_due_at ← now + reinforcement_days, attempts_since_last_pass ← 0.
        - On fail: increment attempts_since_last_pass; latest_completed_module_id
          stays put.
        Either way, latest_attempt_* always advance.
        """
        ts = attempt_at or _now()
        comp = await self.get(chw_id=chw_id, module_family_id=module_family_id)
        if comp is None:
            comp = CHWModuleCompletion(
                chw_id=chw_id,
                module_family_id=module_family_id,
                tenant_id=tenant_id,
                attempts_since_last_pass=0,
            )
            self._session.add(comp)

        comp.latest_attempt_module_id = attempted_module_id
        comp.latest_attempt_at = ts
        comp.latest_quiz_score = score_pct
        comp.latest_attempt_passed = passed
        if tenant_id is not None and comp.tenant_id is None:
            comp.tenant_id = tenant_id

        if passed:
            comp.latest_completed_module_id = attempted_module_id
            comp.completed_at = ts
            comp.attempts_since_last_pass = 0
            comp.reinforcement_due_at = ts + timedelta(days=reinforcement_days)
        else:
            comp.attempts_since_last_pass = (comp.attempts_since_last_pass or 0) + 1
            # On fail, leave reinforcement_due_at as-is — the module stays in
            # rotation until the next pass.

        await self._session.flush()
        return comp

    async def mark_completed(
        self,
        *,
        chw_id: int,
        module_family_id: UUID,
        completed_module_id: UUID,
        completed_at: datetime | None = None,
    ) -> CHWModuleCompletion | None:
        """Stamp the completion timestamp without changing quiz state.

        No-op if the completion row doesn't exist (the quiz attempt should have
        created it; if it didn't, we skip rather than fabricate a row with no
        score data).
        """
        comp = await self.get(chw_id=chw_id, module_family_id=module_family_id)
        if comp is None:
            return None
        comp.latest_completed_module_id = completed_module_id
        comp.completed_at = completed_at or _now()
        await self._session.flush()
        return comp

    async def clear_completion_stamp(
        self,
        *,
        chw_id: int,
        module_family_id: UUID,
    ) -> CHWModuleCompletion | None:
        """Clear per-question completion stamp when quiz coverage drops below 100%."""
        comp = await self.get(chw_id=chw_id, module_family_id=module_family_id)
        if comp is None or comp.completed_at is None:
            return comp
        comp.completed_at = None
        comp.latest_completed_module_id = None
        await self._session.flush()
        return comp

    async def list_due_for_reinforcement(
        self, *, chw_id: int, now: datetime | None = None
    ) -> list[CHWModuleCompletion]:
        """Modules whose reinforcement_due_at has passed (back into rotation)."""
        ts = now or _now()
        result = await self._session.execute(
            select(CHWModuleCompletion)
            .where(
                CHWModuleCompletion.chw_id == chw_id,
                CHWModuleCompletion.reinforcement_due_at <= ts,
            )
            .order_by(CHWModuleCompletion.reinforcement_due_at)
        )
        return list(result.scalars().all())

    async def list_for_chw(self, chw_id: int) -> list[CHWModuleCompletion]:
        result = await self._session.execute(
            select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw_id)
        )
        return list(result.scalars().all())

    async def list_for_chw_updated_since(
        self,
        *,
        chw_id: int,
        since: datetime | None,
    ) -> list[CHWModuleCompletion]:
        stmt = select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw_id)
        if since is not None:
            stmt = stmt.where(
                or_(
                    CHWModuleCompletion.latest_attempt_at > since,
                    CHWModuleCompletion.completed_at > since,
                    CHWModuleCompletion.reinforcement_due_at > since,
                )
            )
        stmt = stmt.order_by(CHWModuleCompletion.module_family_id.asc())
        return list((await self._session.execute(stmt)).scalars().all())

"""W-8 — Module selector.

Given a CHW and a set of triggers that have currently fired, return the
modules to surface — sorted by priority and filtered to skip:
- Modules the CHW completed within their periodic_refresh window
- Modules under suppressed/deprecated triggers

Edge cases (Implementation Plan §10):
3. Multiple modules bound to same gap → priority_weight selects winner
4. Module bound to gap but already completed within periodic_refresh → next-priority
5. All modules for a gap completed → gap suppressed (returns empty for that trigger)
6. Workflow event fires but no module bound → no-op
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.repositories.trigger_repository import TriggerRepository


@dataclass(frozen=True)
class ModuleCandidate:
    """One module surfaced for a CHW, with the trigger that selected it."""

    module_family_id: UUID
    trigger_code: str
    priority_weight: int
    relationship: str  # primary | secondary
    skipped_due_to_completion: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


class ModuleSelector:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trigger_repo = TriggerRepository(session)

    async def select_modules_for_chw(
        self,
        *,
        chw_id: int,
        fired_trigger_codes: list[str],
        now: datetime | None = None,
    ) -> list[ModuleCandidate]:
        """Return module candidates ordered by descending priority_weight.

        For each fired trigger, gather its bindings, drop modules in their
        periodic_refresh window, then merge across triggers — when the same
        module is reached via multiple triggers, the higher priority_weight
        wins (a module appears at most once in the output).
        """
        if not fired_trigger_codes:
            return []
        bindings_with_trigger = await self._trigger_repo.list_active_bindings_for_trigger_codes(
            fired_trigger_codes
        )
        if not bindings_with_trigger:
            return []  # Edge case 6: workflow event with no binding → no-op

        # Per-CHW completion lookup: module_family_id → completion row.
        completions = await self._load_completions(
            chw_id, [b.module_family_id for b, _ in bindings_with_trigger]
        )
        ts = now or _now()

        out: dict[UUID, ModuleCandidate] = {}
        for binding, trigger in bindings_with_trigger:
            comp = completions.get(binding.module_family_id)
            in_refresh_window = _within_refresh_window(comp, ts)
            cand = ModuleCandidate(
                module_family_id=binding.module_family_id,
                trigger_code=trigger.trigger_code,
                priority_weight=binding.priority_weight,
                relationship=binding.relationship,
                skipped_due_to_completion=in_refresh_window,
            )
            existing = out.get(binding.module_family_id)
            if existing is None or binding.priority_weight > existing.priority_weight:
                out[binding.module_family_id] = cand

        # Drop entries skipped due to completion; sort survivors by priority desc.
        active = [c for c in out.values() if not c.skipped_due_to_completion]
        active.sort(key=lambda c: c.priority_weight, reverse=True)
        return active

    async def _load_completions(
        self, chw_id: int, module_family_ids: list[UUID]
    ) -> dict[UUID, CHWModuleCompletion]:
        if not module_family_ids:
            return {}
        result = await self._session.execute(
            select(CHWModuleCompletion).where(
                CHWModuleCompletion.chw_id == chw_id,
                CHWModuleCompletion.module_family_id.in_(module_family_ids),
            )
        )
        return {row.module_family_id: row for row in result.scalars().all()}


def _within_refresh_window(comp: CHWModuleCompletion | None, now: datetime) -> bool:
    """A module is 'in its refresh window' (i.e., suppressed from the
    morning rotation) if the CHW passed it and the next reinforcement is
    not yet due."""
    if comp is None:
        return False
    if not comp.latest_attempt_passed:
        return False
    if comp.reinforcement_due_at is None:
        # Passed but no due date set → suppress until operator sets one.
        return True
    return comp.reinforcement_due_at > now

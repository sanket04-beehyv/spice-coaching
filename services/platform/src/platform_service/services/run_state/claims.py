"""Pipeline run claim acquire / refresh / release."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.services.run_state.constants import (
    _DEFAULT_CLAIM_STALE_SECONDS,
    _PIPELINE_CLAIM_KEY,
    now_utc,
)


class RunClaimMixin:
    _session: AsyncSession

    async def try_claim_run(
        self,
        run_id: UUID,
        *,
        claim_token: str,
        stale_after_seconds: int = _DEFAULT_CLAIM_STALE_SECONDS,
    ) -> bool:
        """Atomically claim a resumable run for this worker."""
        now = now_utc()
        stale_before = now - timedelta(seconds=stale_after_seconds)
        result = await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = COALESCE(error_jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        CAST(:claim_key AS text),
                        jsonb_build_object(
                            'claim_token', :claim_token,
                            -- CAST avoids asyncpg indeterminate-datatype bind on
                            -- jsonb_build_object; value is ISO-8601 text in JSON.
                            'claimed_at', CAST(:claimed_at AS text)
                        )
                    )
                WHERE id = :run_id
                  AND status IN ('running', 'partially_succeeded')
                  AND (
                    error_jsonb IS NULL
                    OR error_jsonb->CAST(:claim_key AS text) IS NULL
                    OR error_jsonb->CAST(:claim_key AS text)->>'claim_token' = :claim_token
                    -- Stale-takeover branch: compare heartbeat as timestamptz,
                    -- not lexicographic text (formats must stay ISO-8601).
                    OR CAST(error_jsonb->CAST(:claim_key AS text)->>'claimed_at' AS timestamptz)
                       < CAST(:stale_before AS timestamptz)
                  )
                RETURNING id
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
                "claimed_at": now.isoformat(),
                "stale_before": stale_before,
            },
        )
        return result.scalar_one_or_none() is not None

    async def refresh_run_claim(self, run_id: UUID, *, claim_token: str) -> bool:
        """Extend the claim heartbeat while this worker is still driving the run."""
        now = now_utc()
        result = await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = COALESCE(error_jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        CAST(:claim_key AS text),
                        jsonb_build_object(
                            'claim_token', :claim_token,
                            -- CAST avoids asyncpg indeterminate-datatype bind on
                            -- jsonb_build_object; value is ISO-8601 text in JSON.
                            'claimed_at', CAST(:claimed_at AS text)
                        )
                    )
                WHERE id = :run_id
                  AND error_jsonb->CAST(:claim_key AS text)->>'claim_token'
                      = CAST(:claim_token AS text)
                RETURNING id
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
                "claimed_at": now.isoformat(),
            },
        )
        return result.scalar_one_or_none() is not None

    async def release_run_claim(self, run_id: UUID, *, claim_token: str) -> None:
        """Drop the pipeline claim when this worker finishes or aborts."""
        await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = error_jsonb - CAST(:claim_key AS text)
                WHERE id = :run_id
                  AND error_jsonb->CAST(:claim_key AS text)->>'claim_token'
                      = CAST(:claim_token AS text)
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
            },
        )

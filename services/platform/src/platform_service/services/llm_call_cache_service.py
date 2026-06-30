"""W-7 — llm_call_cache service + caching wrapper around AIRuntimeClient.

Per Pipeline §16 P3 / Data Model §4.3. Hashes (model + prompt + input
payload) and stores the parsed ai-runtime response in `llm_call_cache`.
On a re-run after failure, the stage worker computes the same hash, gets a
cache hit, and skips the LLM round-trip entirely.

The cache is keyed by hash so duplicate inputs (same prompt + same model +
same payload) deduplicate naturally — no per-request_id key.

Retention is handled by the staging-cleanup job (default 30 days). Stale
hits after eviction simply trigger a fresh LLM call.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from mc_contracts.internal_ai import InferenceRequest, InferenceResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.llm_call_cache import LlmCallCache
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient

logger = logging.getLogger(__name__)


def _stored_response_has_error(response_jsonb: dict[str, Any]) -> bool:
    """Return True when a cached ai-runtime payload recorded a provider/parse failure."""
    error = response_jsonb.get("error")
    return isinstance(error, str) and bool(error.strip())


def compute_input_hash(request: InferenceRequest) -> str:
    """Stable SHA-256 of (generation_type, model, prompt template + content,
    context payload, image attachments).

    JSON-serialise with sort_keys so equivalent dicts hash identically. Excludes
    request_id and trace_context so different runs of the same logical prompt
    collide deterministically and hit the cache.

    Image attachment bytes are hashed (not embedded) to keep the key small —
    two requests with the same MIME type and the same image content collide.
    """
    settings = get_settings()
    payload = {
        "generation_type": request.generation_type.value,
        "model": {
            # Provider is ai-runtime config; platform ai_cloud_provider must match.
            "provider": settings.ai_cloud_provider,
            "model": request.model_policy.model,
        },
        "prompt": {
            "template_id": request.prompt.template_id,
            "template_version": request.prompt.template_version,
            "system": request.prompt.resolved_system_prompt,
            "human": request.prompt.resolved_human_message,
        },
        "constraints": {
            "language": request.constraints.language,
            "output_format": request.constraints.output_format,
            "max_tokens": request.constraints.max_tokens,
            "temperature": request.constraints.temperature,
        },
        "context": request.context,
        "image_attachments": [
            # `label` is descriptive metadata (e.g. "{source_doc_id}/page_n")
            # used for tracing, not content. Excluded from the hash so two
            # uploads of identical bytes with different labels collide.
            {
                "mime_type": a.mime_type,
                "sha256": hashlib.sha256(a.data_base64.encode()).hexdigest(),
            }
            for a in (request.image_attachments or [])
        ],
    }
    serialised = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


class LlmCallCacheService:
    """Read/write helper around the llm_call_cache table.

    Reads use the orchestrator's session (cheap, transactional consistency
    is fine for "did we already see this hash"). Writes use a *fresh* short-
    lived session that commits independently — the cache must survive
    rollbacks of the orchestrator's stage-level transaction. Without this,
    a Stage 1/2/3 failure wipes every cached LLM call from that run on
    rollback, defeating the purpose of caching for retry.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, input_hash: str) -> LlmCallCache | None:
        result = await self._session.execute(
            select(LlmCallCache).where(LlmCallCache.input_hash == input_hash)
        )
        return result.scalar_one_or_none()

    async def put(
        self,
        *,
        input_hash: str,
        model: str,
        response_jsonb: dict[str, Any],
        token_usage: dict[str, Any] | None = None,
        prompt_template_id: UUID | None = None,
    ) -> LlmCallCache:
        async with SessionLocal() as own_session:
            row = LlmCallCache(
                input_hash=input_hash,
                model=model,
                response_jsonb=response_jsonb,
                token_usage_jsonb=token_usage,
                prompt_template_id=prompt_template_id,
            )
            own_session.add(row)
            try:
                await own_session.commit()
            except IntegrityError:
                # Concurrent insert — another worker won the race. Discard
                # our pending add and return the row another writer landed.
                await own_session.rollback()
                existing = await self.get(input_hash)
                if existing is None:
                    raise
                return existing
            return row


class CachingAIRuntimeClient:
    """Drop-in wrapper around AIRuntimeClient that consults llm_call_cache.

    Stage A/B/C/D drafters call `.generate()` on whatever client they were
    constructed with. Pass an instance of this wrapper to opt those drafters
    into caching — no other code change required.

    Embeddings are NOT cached (they're cheap and ai-runtime serves them out
    of a different code path).
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        inner: AIRuntimeClient | None = None,
    ) -> None:
        self._cache = LlmCallCacheService(session)
        self._inner = inner

    @property
    def inner(self) -> AIRuntimeClient:
        """Shared httpx client; lazily resolved from the process singleton."""
        if self._inner is None:
            self._inner = get_ai_client()
        return self._inner

    async def generate(self, request: InferenceRequest) -> InferenceResponse:
        input_hash = compute_input_hash(request)
        hit = await self._cache.get(input_hash)
        if hit is not None and not _stored_response_has_error(hit.response_jsonb):
            logger.info(
                "llm_call_cache HIT generation_type=%s hash=%s",
                request.generation_type.value,
                input_hash[:12],
            )
            return InferenceResponse.model_validate(hit.response_jsonb)
        if hit is not None:
            logger.info(
                "llm_call_cache SKIP_HIT (stored error) generation_type=%s hash=%s",
                request.generation_type.value,
                input_hash[:12],
            )

        response = await self.inner.generate(request)
        if response.error:
            logger.info(
                "llm_call_cache MISS not_stored (error) generation_type=%s hash=%s",
                request.generation_type.value,
                input_hash[:12],
            )
            return response

        await self._cache.put(
            input_hash=input_hash,
            model=response.model or request.model_policy.model,
            response_jsonb=response.model_dump(mode="json"),
            token_usage={
                "input": response.token_usage.input,
                "output": response.token_usage.output,
            },
        )
        logger.info(
            "llm_call_cache MISS+stored generation_type=%s hash=%s",
            request.generation_type.value,
            input_hash[:12],
        )
        return response

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Embeddings bypass cache.
        return await self.inner.embed(texts)

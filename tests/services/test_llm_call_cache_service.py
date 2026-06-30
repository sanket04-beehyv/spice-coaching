"""W-7 — LlmCallCacheService + CachingAIRuntimeClient tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceImage,
    InferenceRequest,
    InferenceResponse,
    ModelPolicy,
    PromptSpec,
    TokenUsage,
)
from platform_service.db.models.llm_call_cache import LlmCallCache
from platform_service.services.llm_call_cache_service import (
    CachingAIRuntimeClient,
    LlmCallCacheService,
    compute_input_hash,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

# ── compute_input_hash (pure unit) ──────────────────────────────────────


def _make_request(
    *,
    prompt_text: str = "hi",
    template_id: str = "t",
    template_version: int = 1,
    context: dict | None = None,
    image: InferenceImage | None = None,
    request_id: str | None = None,
) -> InferenceRequest:
    return InferenceRequest(
        request_id=request_id or str(uuid4()),
        generation_type=GenerationType.CARD_DRAFTING,
        model_policy=ModelPolicy(model="gemini-2.5-flash"),
        prompt=PromptSpec(
            template_id=template_id,
            template_version=template_version,
            resolved_system_prompt="sys",
            resolved_human_message=prompt_text,
        ),
        constraints=GenerationConstraints(),
        context=context or {},
        image_attachments=[image] if image else [],
    )


def test_hash_identical_for_same_logical_request() -> None:
    r1 = _make_request(request_id="req-A")
    r2 = _make_request(request_id="req-B")
    assert compute_input_hash(r1) == compute_input_hash(r2)


def test_hash_differs_for_different_prompt() -> None:
    r1 = _make_request(prompt_text="alpha")
    r2 = _make_request(prompt_text="beta")
    assert compute_input_hash(r1) != compute_input_hash(r2)


def test_hash_differs_for_different_template_version() -> None:
    r1 = _make_request(template_version=1)
    r2 = _make_request(template_version=2)
    assert compute_input_hash(r1) != compute_input_hash(r2)


def test_hash_differs_for_different_context_payload() -> None:
    r1 = _make_request(context={"k": 1})
    r2 = _make_request(context={"k": 2})
    assert compute_input_hash(r1) != compute_input_hash(r2)


def test_hash_stable_under_dict_key_reordering() -> None:
    r1 = _make_request(context={"a": 1, "b": 2})
    r2 = _make_request(context={"b": 2, "a": 1})
    assert compute_input_hash(r1) == compute_input_hash(r2)


def test_hash_differs_for_different_image() -> None:
    img1 = InferenceImage(mime_type="image/png", data_base64="aGVsbG8=")
    img2 = InferenceImage(mime_type="image/png", data_base64="d29ybGQ=")
    r1 = _make_request(image=img1)
    r2 = _make_request(image=img2)
    assert compute_input_hash(r1) != compute_input_hash(r2)


# ── LlmCallCacheService put / get (integration) ─────────────────────────


def _make_response(request: InferenceRequest, *, raw: str = "out") -> InferenceResponse:
    return InferenceResponse(
        request_id=request.request_id,
        generation_type=request.generation_type,
        provider="google",
        model="gemini-2.5-flash",
        raw_text=raw,
        parsed_json={"k": "v"},
        latency_ms=42,
        token_usage=TokenUsage(input=10, output=20),
    )


@pytest.mark.asyncio
@requires_db
async def test_put_then_get_round_trips(db_session: AsyncSession) -> None:
    svc = LlmCallCacheService(db_session)
    h = f"hash-{uuid4().hex}"
    row = await svc.put(
        input_hash=h,
        model="gemini-2.5-flash",
        response_jsonb={"raw_text": "out"},
    )
    assert row.input_hash == h
    fetched = await svc.get(h)
    assert fetched is not None
    assert fetched.input_hash == h
    assert fetched.response_jsonb == {"raw_text": "out"}


@pytest.mark.asyncio
@requires_db
async def test_get_unknown_hash_returns_none(db_session: AsyncSession) -> None:
    svc = LlmCallCacheService(db_session)
    assert await svc.get(f"missing-{uuid4().hex}") is None


# ── CachingAIRuntimeClient (integration) ───────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_caching_client_first_call_misses_then_caches(db_session: AsyncSession) -> None:
    """First call hits the inner client and stores; second call returns from cache
    without invoking the inner client."""
    inner = AsyncMock()
    request = _make_request(prompt_text=f"unique-{uuid4().hex}")
    inner.generate.return_value = _make_response(request)
    cli = CachingAIRuntimeClient(session=db_session, inner=inner)

    resp1 = await cli.generate(request)
    assert resp1.raw_text == "out"
    assert inner.generate.call_count == 1

    resp2 = await cli.generate(request)
    assert resp2.raw_text == "out"
    # Second call must NOT have invoked the inner client.
    assert inner.generate.call_count == 1


@pytest.mark.asyncio
@requires_db
async def test_caching_client_different_inputs_both_hit_inner(db_session: AsyncSession) -> None:
    inner = AsyncMock()
    inner.generate.side_effect = lambda req: _make_response(req, raw=req.prompt.resolved_human_message)
    cli = CachingAIRuntimeClient(session=db_session, inner=inner)

    r_a = _make_request(prompt_text=f"alpha-{uuid4().hex}")
    r_b = _make_request(prompt_text=f"beta-{uuid4().hex}")
    resp_a = await cli.generate(r_a)
    resp_b = await cli.generate(r_b)
    assert resp_a.raw_text != resp_b.raw_text
    assert inner.generate.call_count == 2


@pytest.mark.asyncio
@requires_db
async def test_caching_client_does_not_store_error_responses(db_session: AsyncSession) -> None:
    """Failed ai-runtime responses must not be cached — transient errors would
    otherwise be replayed forever on cache HIT."""
    inner = AsyncMock()
    request = _make_request(prompt_text=f"err-{uuid4().hex}")
    inner.generate.return_value = InferenceResponse(
        request_id=request.request_id,
        generation_type=request.generation_type,
        provider="google",
        model="gemini-2.5-flash",
        raw_text="",
        parsed_json=None,
        latency_ms=1,
        error="quota exceeded",
    )
    cli = CachingAIRuntimeClient(session=db_session, inner=inner)

    resp = await cli.generate(request)
    assert resp.error == "quota exceeded"
    assert inner.generate.call_count == 1

    # Second call must re-invoke inner — no error HIT.
    resp2 = await cli.generate(request)
    assert resp2.error == "quota exceeded"
    assert inner.generate.call_count == 2


@pytest.mark.asyncio
@requires_db
async def test_caching_client_skips_hit_when_cached_row_has_error(
    db_session: AsyncSession,
) -> None:
    """Legacy rows that stored an error must be ignored so a fresh LLM call runs."""
    inner = AsyncMock()
    request = _make_request(prompt_text=f"legacy-err-{uuid4().hex}")
    ok_response = _make_response(request, raw="recovered")
    inner.generate.return_value = ok_response
    cli = CachingAIRuntimeClient(session=db_session, inner=inner)

    input_hash = compute_input_hash(request)
    svc = LlmCallCacheService(db_session)
    await svc.put(
        input_hash=input_hash,
        model="gemini-2.5-flash",
        response_jsonb={
            "request_id": request.request_id,
            "generation_type": request.generation_type.value,
            "provider": "google",
            "model": "gemini-2.5-flash",
            "raw_text": "",
            "parsed_json": None,
            "latency_ms": 1,
            "error": "failed to parse JSON from provider output",
        },
    )

    resp = await cli.generate(request)
    assert resp.raw_text == "recovered"
    inner.generate.assert_awaited_once()


@pytest.mark.asyncio
@requires_db
async def test_caching_client_embed_bypasses_cache(db_session: AsyncSession) -> None:
    inner = AsyncMock()
    inner.embed.return_value = [[0.1, 0.2]]
    cli = CachingAIRuntimeClient(session=db_session, inner=inner)
    out = await cli.embed(["a"])
    assert out == [[0.1, 0.2]]
    inner.embed.assert_called_once_with(["a"])


# ── Layer 2 regressions for bugs caught in the smoke loop ─────────────────


def test_hash_independent_of_image_label() -> None:
    """Same image bytes + different `label` should hash identically.

    `label` is descriptive metadata (a debugging tag like
    "{source_document_id}/page_{n}"); it is NOT content. Including it in the
    cache key makes every re-upload of the same PDF miss cache, defeating
    the purpose of caching for retry.
    """
    img_a = InferenceImage(mime_type="image/png", data_base64="aGVsbG8=", label="upload_a/page_1")
    img_b = InferenceImage(mime_type="image/png", data_base64="aGVsbG8=", label="upload_b/page_1")
    r1 = _make_request(image=img_a)
    r2 = _make_request(image=img_b)
    assert compute_input_hash(r1) == compute_input_hash(r2)


@pytest.mark.asyncio
async def test_put_uses_own_session_not_orchestrators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LlmCallCacheService.put MUST write through SessionLocal (its own
    short-lived session), not the orchestrator session it was constructed
    with. This is what makes the cache survive rollbacks of the
    orchestrator-side transaction (see the smoke-loop bug).

    Spy on a fake "orchestrator session" and ensure no add/commit/flush
    methods are called on it; the writes go to the SessionLocal-derived
    session instead.
    """
    orch_session = MagicMock(name="orchestrator_session")
    orch_session.add = MagicMock()
    orch_session.flush = AsyncMock()
    orch_session.commit = AsyncMock()
    orch_session.rollback = AsyncMock()

    own_session = MagicMock(name="own_session")
    own_session.add = MagicMock()
    own_session.commit = AsyncMock()
    own_session.rollback = AsyncMock()
    own_session.execute = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return own_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "platform_service.services.llm_call_cache_service.SessionLocal",
        lambda: _Ctx(),
    )

    svc = LlmCallCacheService(orch_session)
    await svc.put(
        input_hash=f"hash-{uuid4().hex}",
        model="gemini-2.5-flash",
        response_jsonb={"raw_text": "x"},
    )

    # Orchestrator session was never touched by the write path.
    orch_session.add.assert_not_called()
    orch_session.flush.assert_not_called()
    orch_session.commit.assert_not_called()
    # Own session received exactly one add + one commit.
    own_session.add.assert_called_once()
    own_session.commit.assert_awaited_once()


@pytest.mark.asyncio
@requires_db
async def test_cache_row_survives_orchestrator_rollback(
    db_session: AsyncSession,
) -> None:
    """Regression for the smoke-loop bug: a Stage 1/2/3 failure used to
    rollback the orchestrator session, taking every cache row from the
    same run with it. After the fix, cache writes commit on their own
    SessionLocal-derived session.

    Test: write a cache row through the service while the orchestrator
    session has uncommitted state, then rollback the orchestrator session,
    then read the row back. It must still exist.
    """
    unique_hash = f"survive-rollback-{uuid4().hex}"

    # `db_session` plays the role of the orchestrator's session.
    svc = LlmCallCacheService(db_session)
    await svc.put(
        input_hash=unique_hash,
        model="gemini-2.5-flash",
        response_jsonb={"raw_text": "I should outlive a rollback"},
    )

    # Now rollback the orchestrator's session — simulates a stage failure.
    await db_session.rollback()

    # Read the row back through the same session (fresh implicit txn).
    result = await db_session.execute(select(LlmCallCache).where(LlmCallCache.input_hash == unique_hash))
    row = result.scalar_one_or_none()
    assert row is not None, (
        "Cache row was wiped by orchestrator rollback — LlmCallCacheService.put "
        "is sharing the orchestrator session again. Restore the SessionLocal "
        "context-manager pattern in put()."
    )
    assert row.model == "gemini-2.5-flash"
    assert row.response_jsonb == {"raw_text": "I should outlive a rollback"}

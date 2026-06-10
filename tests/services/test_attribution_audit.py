"""Tests for the savepoint-isolated attribution audit writer."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from platform_service.services.attribution_audit import record_attribution_event

pytestmark = pytest.mark.asyncio


def _async_ctx(enter_value=None) -> MagicMock:
    """An object usable as ``async with`` that returns ``enter_value``."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=enter_value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


async def test_uses_savepoint_when_session_in_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.in_transaction.return_value = True
    savepoint = _async_ctx()
    outer_tx = _async_ctx()
    session.begin_nested = MagicMock(return_value=savepoint)
    session.begin = MagicMock(return_value=outer_tx)

    fake_repo = MagicMock()
    fake_repo.append = AsyncMock()
    monkeypatch.setattr(
        "platform_service.services.attribution_audit.AttributionEventRepository",
        lambda _session: fake_repo,
    )

    await record_attribution_event(session, event_type="test", actor="alice")

    session.begin_nested.assert_called_once()
    session.begin.assert_not_called()
    fake_repo.append.assert_awaited_once()
    savepoint.__aenter__.assert_awaited_once()
    savepoint.__aexit__.assert_awaited_once()


async def test_uses_begin_when_session_has_no_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.in_transaction.return_value = False
    savepoint = _async_ctx()
    outer_tx = _async_ctx()
    session.begin_nested = MagicMock(return_value=savepoint)
    session.begin = MagicMock(return_value=outer_tx)

    fake_repo = MagicMock()
    fake_repo.append = AsyncMock()
    monkeypatch.setattr(
        "platform_service.services.attribution_audit.AttributionEventRepository",
        lambda _session: fake_repo,
    )

    await record_attribution_event(session, event_type="test", actor="alice")

    session.begin.assert_called_once()
    session.begin_nested.assert_not_called()
    fake_repo.append.assert_awaited_once()


async def test_swallows_exceptions_so_primary_flow_is_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.in_transaction.return_value = True
    savepoint = _async_ctx()
    session.begin_nested = MagicMock(return_value=savepoint)

    fake_repo = MagicMock()
    fake_repo.append = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "platform_service.services.attribution_audit.AttributionEventRepository",
        lambda _session: fake_repo,
    )

    # Should NOT raise.
    await record_attribution_event(session, event_type="test", actor="alice")
    fake_repo.append.assert_awaited_once()


async def test_passes_kwargs_through_to_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    session = MagicMock()
    session.in_transaction.return_value = True
    session.begin_nested = MagicMock(return_value=_async_ctx())

    fake_repo = MagicMock()
    fake_repo.append = AsyncMock()
    monkeypatch.setattr(
        "platform_service.services.attribution_audit.AttributionEventRepository",
        lambda _session: fake_repo,
    )

    doc_id = uuid4()
    mod_id = uuid4()
    await record_attribution_event(
        session,
        event_type="ingest_completed",
        actor="system",
        source_document_id=doc_id,
        module_id=mod_id,
        payload={"final_status": "succeeded"},
    )

    fake_repo.append.assert_awaited_once_with(
        event_type="ingest_completed",
        actor="system",
        source_document_id=doc_id,
        module_id=mod_id,
        payload={"final_status": "succeeded"},
    )

"""Unit tests for TeamActivityService."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from mc_foundation.problem import AppError
from platform_service.services.team_activity_service import TeamActivityService, _count_refreshers

ORGANIZER_ID = 401


@pytest.fixture
def ch_client() -> MagicMock:
    client = MagicMock()
    client.query_rows = AsyncMock(return_value=[])
    return client


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


def test_count_refreshers_incorrect_then_correct() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 1}


def test_count_refreshers_repeat_incorrect_before_correct() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 1}


def test_count_refreshers_incorrect_only_stays_open() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 0}


def test_count_refreshers_correct_without_prior_incorrect() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
        ]
    )
    assert counts == {}


def test_count_refreshers_interleaved_quiz_ids() -> None:
    quiz_a = uuid4()
    quiz_b = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
            {"chw_id": 1, "quiz_id": quiz_b, "outcome": "incorrect"},
        ]
    )
    assert counts[1] == {"generated": 2, "completed": 1}


def test_count_refreshers_skips_null_quiz_id_and_outcome() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": None, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": None},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 1}


def test_count_refreshers_ignores_other_outcomes_while_open() -> None:
    quiz_a = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "skipped"},
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "correct"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 1}


def test_count_refreshers_multi_chw() -> None:
    quiz_a = uuid4()
    quiz_b = uuid4()
    counts = _count_refreshers(
        [
            {"chw_id": 1, "quiz_id": quiz_a, "outcome": "incorrect"},
            {"chw_id": 2, "quiz_id": quiz_b, "outcome": "incorrect"},
            {"chw_id": 2, "quiz_id": quiz_b, "outcome": "correct"},
        ]
    )
    assert counts[1] == {"generated": 1, "completed": 0}
    assert counts[2] == {"generated": 1, "completed": 1}


async def test_empty_team_returns_zero_summary(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: [],
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    assert resp.summary.total_users == 0
    assert resp.summary.active_users == 0
    assert resp.summary.non_active_users == 0
    assert resp.users == []
    ch_client.query_rows.assert_not_awaited()


async def test_unrestricted_organizer_uses_all_sk_users(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """organizer_id=None loads the full SK roster (auth-off path)."""
    all_sks = [
        {"id": 395, "name": "Alpha SK", "role": "SK", "parent_id": ORGANIZER_ID},
        {"id": 394, "name": "Beta SK", "role": "SK", "parent_id": 999},
    ]
    team_mock = MagicMock(return_value=[all_sks[0]])
    all_sk_mock = MagicMock(return_value=all_sks)
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        team_mock,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_all_sk_users",
        all_sk_mock,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    all_sk_mock.assert_called_once_with()
    team_mock.assert_not_called()
    assert resp.summary.total_users == 2
    assert [u.user_id for u in resp.users] == [395, 394]


async def test_summary_active_and_chatbot_flags(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        {
            "id": 395,
            "name": "Alpha SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
        {
            "id": 394,
            "name": "Beta SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
    ]
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: members,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    async def query_side_effect(query: str, parameters: dict | None = None) -> list[dict]:
        _ = query
        if parameters and "chw_ids" in parameters:
            if "coaching_events" in query:
                return []
            if "chw_digital_help_daily" in query and "last_chat_date" in query:
                return []
            if "chw_digital_help_daily" in query:
                return []
            if "chw_daily_summary" in query and "last_active_date" in query:
                return []
            return [
                {"chw_id": 395, "is_active": 1, "is_chatbot_engaged": 1, "chatbot_query_count": 3},
                {"chw_id": 394, "is_active": 0, "is_chatbot_engaged": 0, "chatbot_query_count": 0},
            ]
        return []

    ch_client.query_rows = AsyncMock(side_effect=query_side_effect)

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    assert resp.summary.total_users == 2
    assert resp.summary.active_users == 1
    assert resp.summary.non_active_users == 1
    assert resp.summary.users_chatbot_engaged == 1
    assert resp.users[0].user_id == 395
    assert resp.users[0].is_active is True
    assert resp.users[0].refreshers_generated == 0
    assert resp.users[0].refreshers_completed == 0
    assert resp.users[1].is_active is False


async def test_chatbot_unattributed_queries(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        {
            "id": 395,
            "name": "Alpha SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
    ]
    module_id = uuid4()
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: members,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    async def query_side_effect(query: str, parameters: dict | None = None) -> list[dict]:
        _ = parameters
        if "coaching_events" in query:
            return []
        if "chw_daily_summary" in query:
            return [{"chw_id": 395, "is_active": 0, "is_chatbot_engaged": 1, "chatbot_query_count": 5}]
        if "chw_digital_help_daily" in query and "last_chat_date" in query:
            return []
        if "chw_digital_help_daily" in query:
            return [
                {"chw_id": 395, "module_id": str(module_id), "query_count": 2},
                {"chw_id": 395, "module_id": None, "query_count": 3},
            ]
        if "chw_daily_summary" in query and "last_active_date" in query:
            return []
        return []

    ch_client.query_rows = AsyncMock(side_effect=query_side_effect)
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleRepository.list_modules_by_ids",
        AsyncMock(return_value=[]),
    )

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    user = resp.users[0]
    assert user.chatbot_query_count == 5
    assert user.chatbot_unattributed_query_count == 3
    assert len(user.chatbot_modules) == 1
    assert user.chatbot_modules[0].module_id == module_id
    assert user.chatbot_modules[0].query_count == 2


async def test_module_completion_in_range(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family_id = uuid4()
    module_id = uuid4()
    members = [
        {
            "id": 395,
            "name": "Alpha SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
    ]
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: members,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value={module_id}),
    )

    completion = MagicMock()
    completion.chw_id = 395
    completion.module_family_id = family_id
    completion.completed_at = datetime(2026, 1, 15, tzinfo=UTC)
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[completion]),
    )

    mod = MagicMock()
    mod.id = module_id
    mod.module_family_id = family_id
    mod.title_localized = {"en": "Test Module"}
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleRepository.list_modules_by_ids",
        AsyncMock(return_value=[mod]),
    )

    ch_client.query_rows = AsyncMock(return_value=[])

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    assert resp.summary.users_completed_module == 1
    assert resp.users[0].has_completed_module_in_range is True
    assert resp.users[0].assigned_modules[0].completed_in_range is True


async def test_last_activity_timestamps(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        {
            "id": 395,
            "name": "Alpha SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
    ]
    last_chat_date = date(2026, 2, 10)
    last_active_date = date(2026, 2, 8)
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: members,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    async def query_side_effect(query: str, parameters: dict | None = None) -> list[dict]:
        _ = parameters
        if "coaching_events" in query:
            return []
        if "chw_digital_help_daily" in query and "last_chat_date" in query:
            return [
                {
                    "chw_id": 395,
                    "last_chat_date": last_chat_date,
                    "last_active_date": last_active_date,
                }
            ]
        if "chw_daily_summary" in query:
            return [{"chw_id": 395, "is_active": 1, "is_chatbot_engaged": 1, "chatbot_query_count": 2}]
        if "chw_digital_help_daily" in query:
            return []
        return []

    ch_client.query_rows = AsyncMock(side_effect=query_side_effect)

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    user = resp.users[0]
    assert user.last_chat_at == datetime(2026, 2, 10, tzinfo=UTC)
    assert user.last_active_at == datetime(2026, 2, 8, tzinfo=UTC)


async def test_refresher_counts_from_coaching_events(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        {
            "id": 395,
            "name": "Alpha SK",
            "role": "SK",
            "parent_id": ORGANIZER_ID,
        },
    ]
    quiz_a = uuid4()
    quiz_b = uuid4()
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: members,
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.resolve_assigned_module_ids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.ModuleCompletionRepository.list_completed_in_range_for_chws",
        AsyncMock(return_value=[]),
    )

    async def query_side_effect(query: str, parameters: dict | None = None) -> list[dict]:
        if "coaching_events" in query:
            assert parameters is not None
            assert parameters["chw_ids"] == [395]
            assert parameters["from_date"] == date(2026, 1, 1)
            assert parameters["to_date"] == date(2026, 1, 31)
            assert "module_quiz_attempted" in query
            assert "ORDER BY chw_id, timestamp_utc ASC, id ASC" in query
            return [
                {
                    "chw_id": 395,
                    "quiz_id": str(quiz_a),
                    "outcome": "incorrect",
                    "timestamp_utc": datetime(2026, 1, 2, tzinfo=UTC),
                    "id": "e1",
                },
                {
                    "chw_id": 395,
                    "quiz_id": str(quiz_a),
                    "outcome": "incorrect",
                    "timestamp_utc": datetime(2026, 1, 3, tzinfo=UTC),
                    "id": "e2",
                },
                {
                    "chw_id": 395,
                    "quiz_id": str(quiz_a),
                    "outcome": "correct",
                    "timestamp_utc": datetime(2026, 1, 4, tzinfo=UTC),
                    "id": "e3",
                },
                {
                    "chw_id": 395,
                    "quiz_id": str(quiz_b),
                    "outcome": "incorrect",
                    "timestamp_utc": datetime(2026, 1, 5, tzinfo=UTC),
                    "id": "e4",
                },
            ]
        if "chw_daily_summary" in query:
            return [{"chw_id": 395, "is_active": 1, "is_chatbot_engaged": 0, "chatbot_query_count": 0}]
        if "chw_digital_help_daily" in query:
            return []
        return []

    ch_client.query_rows = AsyncMock(side_effect=query_side_effect)

    resp = await TeamActivityService(ch_client, session).get_team_activity(
        organizer_id=ORGANIZER_ID,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
        organization_ids=None,
    )

    user = resp.users[0]
    assert user.refreshers_generated == 2
    assert user.refreshers_completed == 1


@pytest.mark.asyncio
async def test_member_questions_forbidden_for_off_team_user(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: [{"id": 395, "name": "A"}],
    )

    with pytest.raises(AppError) as exc_info:
        await TeamActivityService(ch_client, session).get_member_questions(
            organizer_id=ORGANIZER_ID,
            user_id=999,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            limit=50,
            offset=0,
            tenant_id=None,
        )
    assert exc_info.value.status == 403
    ch_client.query_rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_questions_returns_paginated_rows(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: [{"id": 395, "name": "A"}],
    )
    last_asked = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    async def _query_rows(query: str, parameters: dict | None = None) -> list[dict]:
        if "total_questions" in query:
            return [{"total_questions": 3}]
        return [
            {
                "question": "How do I measure RR?",
                "occurrence_count": 2,
                "last_asked_at": last_asked,
            },
            {
                "question": "child fever",
                "occurrence_count": 1,
                "last_asked_at": datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
            },
        ]

    ch_client.query_rows = AsyncMock(side_effect=_query_rows)

    resp = await TeamActivityService(ch_client, session).get_member_questions(
        organizer_id=ORGANIZER_ID,
        user_id=395,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=2,
        offset=0,
        tenant_id=None,
    )

    assert resp.user_id == 395
    assert resp.total_questions == 3
    assert resp.total_pages == 2
    assert resp.limit == 2
    assert len(resp.questions) == 2
    assert resp.questions[0].question == "How do I measure RR?"
    assert resp.questions[0].occurrence_count == 2
    assert resp.questions[0].last_asked_at == last_asked
    assert ch_client.query_rows.await_count == 2
    page_call = ch_client.query_rows.await_args_list[1]
    assert page_call.kwargs["parameters"]["chw_id"] == 395
    assert page_call.kwargs["parameters"]["limit"] == 2
    assert page_call.kwargs["parameters"]["offset"] == 0
    assert "digital_help_used" in page_call.kwargs["parameters"]["event_type"]


@pytest.mark.asyncio
async def test_member_questions_skips_blank_question_rows(
    ch_client: MagicMock,
    session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_service.services.team_activity_service.get_team_members_for_organizer",
        lambda _oid: [{"id": 395, "name": "A"}],
    )

    async def _query_rows(query: str, parameters: dict | None = None) -> list[dict]:
        if "total_questions" in query:
            return [{"total_questions": 1}]
        return [
            {"question": "  ", "occurrence_count": 1, "last_asked_at": datetime(2026, 1, 15, tzinfo=UTC)},
            {
                "question": "fever",
                "occurrence_count": 1,
                "last_asked_at": datetime(2026, 1, 14, tzinfo=UTC),
            },
        ]

    ch_client.query_rows = AsyncMock(side_effect=_query_rows)

    resp = await TeamActivityService(ch_client, session).get_member_questions(
        organizer_id=ORGANIZER_ID,
        user_id=395,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
        limit=50,
        offset=0,
        tenant_id=None,
    )

    assert len(resp.questions) == 1
    assert resp.questions[0].question == "fever"

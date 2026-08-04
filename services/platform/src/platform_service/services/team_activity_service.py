"""Team activity dashboard — organizer-scoped engagement and completion reporting."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from mc_contracts.dashboard import (
    TeamActivityResponse,
    TeamActivitySummary,
    TeamMemberActivityDetail,
    TeamMemberChatbotModuleUsage,
    TeamMemberModuleActivity,
    TeamMemberQuestionItem,
    TeamMemberQuestionsResponse,
)
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.clickhouse.question_sql import QUESTION_EXTRACT_SQL, QUESTION_NORMALIZE_KEY_SQL
from platform_service.db.repositories.module_completion_repository import ModuleCompletionRepository
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.sync.module_assignment_resolver import resolve_assigned_module_ids
from platform_service.services.user_service import get_all_sk_users, get_team_members_for_organizer

_DIGITAL_HELP_EVENT = "digital_help_used"


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def _date_to_datetime_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_datetime(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    return None


def _utc_range_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    from_ts = datetime.combine(from_date, time.min, tzinfo=UTC)
    to_ts = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC) - timedelta(microseconds=1)
    return from_ts, to_ts


def _count_refreshers(
    events: list[dict[str, Any]],
) -> dict[int, dict[str, int]]:
    """Fold ordered quiz attempts into refresher generated/completed counts.

    Events must already be ordered by ``(chw_id, timestamp_utc ASC, id ASC)``.
    Per ``(chw_id, quiz_id)``: first ``incorrect`` opens a refresher; subsequent
    ``incorrect`` while open is a no-op; ``correct`` while open closes it.
    """
    out: dict[int, dict[str, int]] = {}
    open_by_chw: dict[int, set[UUID]] = {}

    for event in events:
        chw_id = _to_int(event.get("chw_id"), default=-1)
        if chw_id < 0:
            continue
        quiz_id = _to_uuid(event.get("quiz_id"))
        outcome = event.get("outcome")
        if quiz_id is None or outcome is None:
            continue
        outcome_str = str(outcome).strip().lower()
        if not outcome_str:
            continue

        if outcome_str == "incorrect":
            bucket = out.setdefault(chw_id, {"generated": 0, "completed": 0})
            open_quizzes = open_by_chw.setdefault(chw_id, set())
            if quiz_id not in open_quizzes:
                open_quizzes.add(quiz_id)
                bucket["generated"] += 1
        elif outcome_str == "correct":
            bucket = out.get(chw_id)
            open_quizzes = open_by_chw.get(chw_id)
            if bucket is None or open_quizzes is None:
                continue
            if quiz_id in open_quizzes:
                open_quizzes.remove(quiz_id)
                bucket["completed"] += 1

    return out


class TeamActivityService:
    def __init__(
        self,
        ch_client: ClickHouseClient,
        session: AsyncSession,
    ) -> None:
        self._ch = ch_client
        self._session = session

    async def get_team_activity(
        self,
        *,
        organizer_id: int | None,
        from_date: date,
        to_date: date,
        limit: int,
        offset: int,
        tenant_id: UUID | None,
        organization_ids: list[int] | None,
    ) -> TeamActivityResponse:
        raw_members = (
            get_all_sk_users() if organizer_id is None else get_team_members_for_organizer(organizer_id)
        )
        members = sorted(raw_members, key=lambda u: u["name"])
        total_users = len(members)
        total_pages = (total_users + limit - 1) // limit if total_users > 0 else 0
        paged_members = members[offset : offset + limit]
        all_chw_ids = [int(m["id"]) for m in members]

        from_ts, to_ts = _utc_range_bounds(from_date, to_date)

        daily_by_chw = await self._fetch_daily_summary(
            chw_ids=all_chw_ids,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
        )

        completions = await ModuleCompletionRepository(self._session).list_completed_in_range_for_chws(
            chw_ids=all_chw_ids,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        completions_by_chw: dict[int, dict[UUID, datetime]] = {}
        for comp in completions:
            if comp.completed_at is not None:
                completions_by_chw.setdefault(comp.chw_id, {})[comp.module_family_id] = comp.completed_at

        assigned_by_chw: dict[int, set[UUID]] = {}
        family_ids_by_chw: dict[int, set[UUID]] = {}
        for member in members:
            chw_id = int(member["id"])
            assigned_ids = await resolve_assigned_module_ids(
                self._session,
                user_id=chw_id,
                organization_ids=organization_ids,
            )
            assigned_by_chw[chw_id] = assigned_ids
            if assigned_ids:
                modules = await ModuleRepository(self._session).list_modules_by_ids(
                    list(assigned_ids),
                    tenant_id=tenant_id,
                )
                family_ids_by_chw[chw_id] = {mod.module_family_id for mod in modules}
            else:
                family_ids_by_chw[chw_id] = set()

        active_users = 0
        users_chatbot_engaged = 0
        users_completed_module = 0
        for member in members:
            chw_id = int(member["id"])
            daily = daily_by_chw.get(chw_id, {})
            if daily.get("is_active"):
                active_users += 1
            if daily.get("is_chatbot_engaged"):
                users_chatbot_engaged += 1
            assigned_families = family_ids_by_chw.get(chw_id, set())
            member_completions = completions_by_chw.get(chw_id, {})
            if assigned_families & member_completions.keys():
                users_completed_module += 1

        summary = TeamActivitySummary(
            total_users=total_users,
            active_users=active_users,
            non_active_users=total_users - active_users,
            users_completed_module=users_completed_module,
            users_chatbot_engaged=users_chatbot_engaged,
        )

        page_chw_ids = [int(m["id"]) for m in paged_members]
        chatbot_by_chw = await self._fetch_digital_help(
            chw_ids=page_chw_ids,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
        )
        last_activity_by_chw = await self._fetch_last_activity_timestamps(
            chw_ids=page_chw_ids,
            tenant_id=tenant_id,
        )
        quiz_attempts = await self._fetch_quiz_attempts(
            chw_ids=page_chw_ids,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
        )
        refreshers_by_chw = _count_refreshers(quiz_attempts)

        all_module_ids: set[UUID] = set()
        for chw_id in page_chw_ids:
            all_module_ids.update(assigned_by_chw.get(chw_id, set()))
            for module_id, _count in chatbot_by_chw.get(chw_id, {}).get("by_module", {}).items():
                if module_id is not None:
                    all_module_ids.add(module_id)

        title_by_module: dict[UUID, Any] = {}
        if all_module_ids:
            modules = await ModuleRepository(self._session).list_modules_by_ids(
                list(all_module_ids),
                tenant_id=tenant_id,
            )
            title_by_module = {mod.id: mod for mod in modules}

        users: list[TeamMemberActivityDetail] = []
        for member in paged_members:
            chw_id = int(member["id"])
            daily = daily_by_chw.get(chw_id, {})
            assigned_ids = assigned_by_chw.get(chw_id, set())
            member_completions = completions_by_chw.get(chw_id, {})
            chatbot = chatbot_by_chw.get(chw_id, {"total": 0, "unattributed": 0, "by_module": {}})
            last_activity = last_activity_by_chw.get(chw_id, {})
            refreshers = refreshers_by_chw.get(chw_id, {"generated": 0, "completed": 0})

            assigned_modules: list[TeamMemberModuleActivity] = []
            for module_id in sorted(assigned_ids, key=str):
                mod = title_by_module.get(module_id)
                family_id = mod.module_family_id if mod else None
                completed_at = member_completions.get(family_id) if family_id else None
                assigned_modules.append(
                    TeamMemberModuleActivity(
                        module_id=module_id,
                        title=mod.title_localized if mod else None,
                        completed_in_range=completed_at is not None,
                        completed_at=completed_at,
                    )
                )

            chatbot_modules: list[TeamMemberChatbotModuleUsage] = []
            for module_id, query_count in sorted(
                chatbot["by_module"].items(),
                key=lambda item: (-item[1], str(item[0])),
            ):
                if module_id is None:
                    continue
                mod = title_by_module.get(module_id)
                chatbot_modules.append(
                    TeamMemberChatbotModuleUsage(
                        module_id=module_id,
                        title=mod.title_localized if mod else None,
                        query_count=query_count,
                    )
                )

            assigned_families = family_ids_by_chw.get(chw_id, set())
            users.append(
                TeamMemberActivityDetail(
                    user_id=chw_id,
                    name=str(member["name"]),
                    is_active=bool(daily.get("is_active")),
                    is_chatbot_engaged=bool(daily.get("is_chatbot_engaged")),
                    last_chat_at=last_activity.get("last_chat_at"),
                    last_active_at=last_activity.get("last_active_at"),
                    has_completed_module_in_range=bool(assigned_families & member_completions.keys()),
                    assigned_modules=assigned_modules,
                    chatbot_query_count=_to_int(chatbot.get("total")),
                    chatbot_unattributed_query_count=_to_int(chatbot.get("unattributed")),
                    chatbot_modules=chatbot_modules,
                    refreshers_generated=_to_int(refreshers.get("generated")),
                    refreshers_completed=_to_int(refreshers.get("completed")),
                )
            )

        return TeamActivityResponse(
            from_date=from_date,
            to_date=to_date,
            summary=summary,
            users=users,
            total_users=total_users,
            total_pages=total_pages,
            limit=limit,
            offset=offset,
            server_time_utc=datetime.now(UTC).isoformat(),
        )

    async def get_member_questions(
        self,
        *,
        organizer_id: int,
        user_id: int,
        from_date: date,
        to_date: date,
        limit: int,
        offset: int,
        tenant_id: UUID | None,
    ) -> TeamMemberQuestionsResponse:
        members = get_team_members_for_organizer(organizer_id)
        member_ids = {int(m["id"]) for m in members}
        if user_id not in member_ids:
            raise AppError(
                ErrorCode.FORBIDDEN.value,
                "user is not a member of this organizer's team",
                status=403,
            )

        total_questions = await self._count_member_questions(
            chw_id=user_id,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
        )
        total_pages = (total_questions + limit - 1) // limit if total_questions > 0 else 0
        rows = await self._fetch_member_questions_page(
            chw_id=user_id,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
            limit=limit,
            offset=offset,
        )

        questions: list[TeamMemberQuestionItem] = []
        for row in rows:
            question = str(row.get("question") or "").strip()
            if not question:
                continue
            last_asked_at = _to_datetime(row.get("last_asked_at"))
            if last_asked_at is None:
                continue
            questions.append(
                TeamMemberQuestionItem(
                    question=question,
                    occurrence_count=_to_int(row.get("occurrence_count")),
                    last_asked_at=last_asked_at,
                )
            )

        return TeamMemberQuestionsResponse(
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
            questions=questions,
            total_questions=total_questions,
            total_pages=total_pages,
            limit=limit,
            offset=offset,
            server_time_utc=datetime.now(UTC).isoformat(),
        )

    def _tenant_clause(self, tenant_id: UUID | None) -> tuple[str, dict[str, Any]]:
        if tenant_id is None:
            return "", {}
        return "  AND tenant_id = {tenant_id:UUID}\n", {"tenant_id": tenant_id}

    async def _count_member_questions(
        self,
        *,
        chw_id: int,
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
    ) -> int:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT count() AS total_questions
        FROM (
          SELECT question_key
          FROM (
            SELECT {QUESTION_NORMALIZE_KEY_SQL} AS question_key
            FROM coaching_events
            WHERE chw_id = {{chw_id:Int64}}
              AND event_type = {{event_type:String}}
              AND event_date >= {{from_date:Date}}
              AND event_date <= {{to_date:Date}}
            {tenant_clause})
          WHERE length(question_key) > 0
          GROUP BY question_key
        )
        """
        parameters: dict[str, Any] = {
            "chw_id": chw_id,
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        if not rows:
            return 0
        return _to_int(rows[0].get("total_questions"))

    async def _fetch_member_questions_page(
        self,
        *,
        chw_id: int,
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          argMax(raw_question, timestamp_utc) AS question,
          count() AS occurrence_count,
          max(timestamp_utc) AS last_asked_at
        FROM (
          SELECT
            {QUESTION_NORMALIZE_KEY_SQL} AS question_key,
            {QUESTION_EXTRACT_SQL} AS raw_question,
            timestamp_utc
          FROM coaching_events
          WHERE chw_id = {{chw_id:Int64}}
              AND event_type = {{event_type:String}}
              AND event_date >= {{from_date:Date}}
              AND event_date <= {{to_date:Date}}
          {tenant_clause})
        WHERE length(question_key) > 0
        GROUP BY question_key
        ORDER BY last_asked_at DESC
        LIMIT {{limit:UInt32}}
        OFFSET {{offset:UInt32}}
        """
        parameters: dict[str, Any] = {
            "chw_id": chw_id,
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            "limit": limit,
            "offset": offset,
            **tenant_params,
        }
        return await self._ch.query_rows(query, parameters=parameters)

    async def _fetch_daily_summary(
        self,
        *,
        chw_ids: list[int],
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
    ) -> dict[int, dict[str, Any]]:
        if not chw_ids:
            return {}
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          chw_id,
          sum(cards_shown + quiz_views + quiz_attempts) > 0 AS is_active,
          sum(digital_help_used) > 0 AS is_chatbot_engaged,
          sum(digital_help_used) AS chatbot_query_count
        FROM chw_daily_summary
        WHERE chw_id IN {{chw_ids:Array(Int64)}}
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
        {tenant_clause}GROUP BY chw_id
        """
        parameters: dict[str, Any] = {
            "chw_ids": chw_ids,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            chw_id = _to_int(row.get("chw_id"), default=-1)
            if chw_id < 0:
                continue
            out[chw_id] = {
                "is_active": bool(row.get("is_active")),
                "is_chatbot_engaged": bool(row.get("is_chatbot_engaged")),
                "chatbot_query_count": _to_int(row.get("chatbot_query_count")),
            }
        return out

    async def _fetch_digital_help(
        self,
        *,
        chw_ids: list[int],
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
    ) -> dict[int, dict[str, Any]]:
        if not chw_ids:
            return {}
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          chw_id,
          module_id,
          sum(query_count) AS query_count
        FROM chw_digital_help_daily
        WHERE chw_id IN {{chw_ids:Array(Int64)}}
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
        {tenant_clause}GROUP BY chw_id, module_id
        """
        parameters: dict[str, Any] = {
            "chw_ids": chw_ids,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            chw_id = _to_int(row.get("chw_id"), default=-1)
            if chw_id < 0:
                continue
            bucket = out.setdefault(
                chw_id,
                {"total": 0, "unattributed": 0, "by_module": {}},
            )
            module_id = _to_uuid(row.get("module_id"))
            count = _to_int(row.get("query_count"))
            bucket["total"] += count
            if module_id is None:
                bucket["unattributed"] += count
            else:
                bucket["by_module"][module_id] = bucket["by_module"].get(module_id, 0) + count
        return out

    async def _fetch_last_activity_timestamps(
        self,
        *,
        chw_ids: list[int],
        tenant_id: UUID | None,
    ) -> dict[int, dict[str, datetime | None]]:
        if not chw_ids:
            return {}
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          coalesce(chat.chw_id, active.chw_id) AS chw_id,
          chat.last_chat_date,
          active.last_active_date
        FROM (
          SELECT
            chw_id,
            max(event_date) AS last_chat_date
          FROM chw_digital_help_daily
          WHERE chw_id IN {{chw_ids:Array(Int64)}}
            AND query_count > 0
          {tenant_clause}GROUP BY chw_id
        ) AS chat
        FULL OUTER JOIN (
          SELECT
            chw_id,
            max(event_date) AS last_active_date
          FROM chw_daily_summary
          WHERE chw_id IN {{chw_ids:Array(Int64)}}
            AND (cards_shown + quiz_views + quiz_attempts) > 0
          {tenant_clause}GROUP BY chw_id
        ) AS active ON chat.chw_id = active.chw_id
        """
        parameters: dict[str, Any] = {
            "chw_ids": chw_ids,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        out: dict[int, dict[str, datetime | None]] = {}
        for row in rows:
            chw_id = _to_int(row.get("chw_id"), default=-1)
            if chw_id < 0:
                continue
            out[chw_id] = {
                "last_chat_at": _date_to_datetime_utc(row.get("last_chat_date")),
                "last_active_at": _date_to_datetime_utc(row.get("last_active_date")),
            }
        return out

    async def _fetch_quiz_attempts(
        self,
        *,
        chw_ids: list[int],
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
    ) -> list[dict[str, Any]]:
        if not chw_ids:
            return []
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          chw_id,
          quiz_id,
          outcome,
          timestamp_utc,
          id
        FROM coaching_events
        WHERE chw_id IN {{chw_ids:Array(Int64)}}
          AND event_type = 'module_quiz_attempted'
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
        {tenant_clause}ORDER BY chw_id, timestamp_utc ASC, id ASC
        """
        parameters: dict[str, Any] = {
            "chw_ids": chw_ids,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        return await self._ch.query_rows(query, parameters=parameters)

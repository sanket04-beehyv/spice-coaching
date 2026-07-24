"""Admin module demand summary: form + chatbot demand, LLM narrative."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mc_contracts.admin_assignments import AssignmentCreateRequest
from mc_contracts.admin_module_demand import (
    ModuleDemandAction,
    ModuleDemandAssignResponse,
    ModuleDemandItem,
    ModuleDemandRequestor,
    ModuleDemandRequestorsResponse,
    ModuleDemandSource,
    ModuleDemandSummaryResponse,
)
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    TraceContext,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.repositories.config_threshold_repository import ConfigThresholdRepository
from platform_service.db.repositories.module_assignment_repository import ModuleAssignmentRepository
from platform_service.db.repositories.module_demand_summary_repository import (
    ModuleDemandSummaryRepository,
)
from platform_service.db.repositories.training_request_repository import TrainingRequestRepository
from platform_service.deps import get_ai_client, get_clickhouse_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.attribution_audit import record_attribution_event
from platform_service.services.dashboard_analytics_service import (
    DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
    DashboardAnalyticsService,
)
from platform_service.services.module_assignment_service import (
    ModuleAssignmentService,
    ModuleNotFoundError,
)
from platform_service.services.prompt_registry import MODULE_DEMAND_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.module_demand_variables import build_module_demand_variables
from platform_service.services.prompts.module_demand_prompt import fallback_summary
from platform_service.services.user_service import get_all_users

logger = logging.getLogger(__name__)

MODULE_DEMAND_TOP_K_KEY = "module_demand_top_k"
MODULE_DEMAND_TOP_K_DEFAULT = 10
MODULE_DEMAND_TOP_K_MAX = 50
# Bound LLM wait on the admin summary path so CH/form data still returns quickly.
MODULE_DEMAND_LLM_TIMEOUT_SECONDS = 8.0
# The daily refresh job can afford a longer LLM wait — it's off the request path.
MODULE_DEMAND_LLM_JOB_TIMEOUT_SECONDS = 60.0


@dataclass
class _DemandBucket:
    display_name: str
    module_id: UUID | None = None
    lifecycle_status: str | None = None
    domain: str | None = None
    chatbot_faqs_only: bool = False
    # Distinct CHWs who requested this module/name (see request_count).
    chw_ids: set[int] = field(default_factory=set)
    matched_names: set[str] = field(default_factory=set)

    @property
    def request_count(self) -> int:
        """Distinct requestor count (unique chw_id), not raw training-request rows."""
        return len(self.chw_ids)


def _display_title(title_localized: dict[str, str] | None, *, preferred: str = "en") -> str:
    if not title_localized:
        return "Untitled module"
    if preferred in title_localized and title_localized[preferred].strip():
        return title_localized[preferred].strip()
    for key in ("en", "bn"):
        value = title_localized.get(key)
        if value and value.strip():
            return value.strip()
    for value in title_localized.values():
        if value and str(value).strip():
            return str(value).strip()
    return "Untitled module"


def _normalize_name(name: str) -> str:
    return name.strip().casefold()


def _title_aliases(title_localized: dict[str, str] | None) -> set[str]:
    aliases: set[str] = set()
    if not title_localized:
        return aliases
    for value in title_localized.values():
        if value and str(value).strip():
            aliases.add(_normalize_name(str(value)))
    return aliases


class ModuleDemandService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        client: AIRuntimeClient | None = None,
        settings: Settings | None = None,
        ch_client: ClickHouseClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._client = client or get_ai_client()
        self._ch = ch_client if ch_client is not None else get_clickhouse_client()
        self._chatbot = DashboardAnalyticsService(self._ch, session)
        self._requests = TrainingRequestRepository(session)
        self._assignments = ModuleAssignmentRepository(session)
        self._configs = ConfigThresholdRepository(session)
        self._summaries = ModuleDemandSummaryRepository(session)

    async def resolve_top_k(self) -> int:
        configured = await self._configs.get_int(MODULE_DEMAND_TOP_K_KEY, MODULE_DEMAND_TOP_K_DEFAULT)
        return max(1, min(configured, MODULE_DEMAND_TOP_K_MAX))

    async def get_summary(
        self,
        *,
        tenant_id: UUID | None = None,
        chatbot_period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
    ) -> ModuleDemandSummaryResponse:
        """Read the daily-precomputed snapshot; compute live on cache miss.

        The snapshot is refreshed by ``refresh_module_demand_summary`` (daily
        Celery beat) so the request path avoids the ClickHouse + ai-runtime
        fan-out. Before the first job run (or in tests) we fall back to a live
        build so the endpoint is never empty.
        """
        cached = await self._summaries.get(tenant_id)
        if cached is not None:
            return ModuleDemandSummaryResponse.model_validate(cached.payload_json)
        return await self.build_summary(
            tenant_id=tenant_id,
            chatbot_period_days=chatbot_period_days,
        )

    async def list_summary_scopes(self) -> list[UUID | None]:
        """Scopes to precompute: the global (None) scope plus each active tenant."""
        scopes: list[UUID | None] = [None]
        scopes.extend(await self._requests.distinct_tenant_ids())
        return scopes

    async def refresh_summary(
        self,
        *,
        tenant_id: UUID | None = None,
        chatbot_period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
    ) -> ModuleDemandSummaryResponse:
        """Compute a fresh summary and persist it as the cached snapshot."""
        summary = await self.build_summary(
            tenant_id=tenant_id,
            chatbot_period_days=chatbot_period_days,
            llm_timeout_seconds=MODULE_DEMAND_LLM_JOB_TIMEOUT_SECONDS,
        )
        await self._summaries.upsert(
            tenant_id=tenant_id,
            top_k=summary.top_k,
            payload_json=summary.model_dump(mode="json"),
            generated_at=summary.generated_at,
            computed_at=datetime.now(UTC),
        )
        await self._session.commit()
        return summary

    async def build_summary(
        self,
        *,
        tenant_id: UUID | None = None,
        chatbot_period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
        llm_timeout_seconds: float | None = None,
    ) -> ModuleDemandSummaryResponse:
        top_k = await self.resolve_top_k()
        buckets = await self._aggregate_buckets(
            tenant_id=tenant_id,
            chatbot_period_days=chatbot_period_days,
        )
        # Rank by distinct requestor count (request_count), then display name.
        ranked = sorted(
            buckets,
            key=lambda b: (-b.request_count, b.display_name.casefold()),
        )[:top_k]

        available: list[ModuleDemandItem] = []
        unavailable: list[ModuleDemandItem] = []
        for bucket in ranked:
            item = self._to_item(bucket)
            if item.action in (ModuleDemandAction.ASSIGN, ModuleDemandAction.OPEN_DRAFT):
                available.append(item)
            else:
                unavailable.append(item)

        llm_summary = await self._generate_llm_summary(
            ranked=ranked,
            top_k=top_k,
            llm_timeout_seconds=llm_timeout_seconds,
            available_count=len(available),
            unavailable_count=len(unavailable),
        )
        return ModuleDemandSummaryResponse(
            top_k=top_k,
            generated_at=datetime.now(UTC),
            llm_summary=llm_summary,
            available=available,
            unavailable=unavailable,
        )

    async def get_requestors(
        self,
        module_id: UUID,
        *,
        tenant_id: UUID | None = None,
        chatbot_period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
    ) -> ModuleDemandRequestorsResponse:
        module = await self._session.get(Module, module_id)
        if module is None:
            raise LookupError(f"Module with ID {module_id} not found")
        if tenant_id is not None and module.tenant_id is not None and module.tenant_id != tenant_id:
            raise LookupError(f"Module with ID {module_id} not found")

        aliases = _title_aliases(module.title_localized)
        rows = await self._requests.list_for_module_demand(
            module_id=module_id,
            matched_names=sorted(aliases),
            tenant_id=tenant_id,
        )
        users_by_id = {u["id"]: u for u in get_all_users()}
        requestors: list[ModuleDemandRequestor] = []
        seen_chw: set[int] = set()

        for row in rows:
            if row.chw_id in seen_chw:
                continue
            seen_chw.add(row.chw_id)
            existing = await self._assignments.find_user_assignment(module_id, row.chw_id)
            user = users_by_id.get(row.chw_id)
            requestors.append(
                ModuleDemandRequestor(
                    chw_id=row.chw_id,
                    chw_name=user["name"] if user else None,
                    source=ModuleDemandSource.FORM,
                    requested_at=row.submitted_at,
                    already_assigned=existing is not None,
                    request_id=row.id,
                )
            )

        # Soft-fail ClickHouse: form requestors still return when analytics are down.
        try:
            chatbot_rows = await self._chatbot.requestors_for_module(
                module_id=module_id,
                tenant_id=tenant_id,
                period_days=chatbot_period_days,
            )
        except Exception:
            logger.exception("Module demand: ClickHouse chatbot requestors failed")
            chatbot_rows = []

        for chw_id, last_seen in chatbot_rows:
            if chw_id in seen_chw:
                # Form request wins when the same CHW appears in both sources.
                continue
            seen_chw.add(chw_id)
            existing = await self._assignments.find_user_assignment(module_id, chw_id)
            user = users_by_id.get(chw_id)
            requested_at = last_seen or datetime.now(UTC)
            if requested_at.tzinfo is None:
                requested_at = requested_at.replace(tzinfo=UTC)
            requestors.append(
                ModuleDemandRequestor(
                    chw_id=chw_id,
                    chw_name=user["name"] if user else None,
                    source=ModuleDemandSource.CHATBOT,
                    requested_at=requested_at,
                    already_assigned=existing is not None,
                    request_id=None,
                )
            )

        return ModuleDemandRequestorsResponse(
            module_id=module_id,
            module_title=_display_title(module.title_localized),
            requestors=requestors,
        )

    async def assign_to_requestors(
        self,
        *,
        module_id: UUID,
        user_ids: list[int],
        assigned_by: int,
        actor: str,
        tenant_id: UUID | None = None,
    ) -> ModuleDemandAssignResponse:
        if tenant_id is not None:
            module = await self._session.get(Module, module_id)
            if module is None or (module.tenant_id is not None and module.tenant_id != tenant_id):
                raise ModuleNotFoundError(f"Module with ID {module_id} not found")

        service = ModuleAssignmentService(self._session)
        result = await service.create_assignments(
            AssignmentCreateRequest(
                module_id=module_id,
                assignment_type="individual",
                user_ids=user_ids,
            ),
            assigned_by,
            commit=False,
        )
        await record_attribution_event(
            self._session,
            event_type="module_demand_assigned",
            actor=actor,
            module_id=module_id,
            payload={
                "user_ids": user_ids,
                "assigned_count": result["assigned_count"],
                "source": "module_demand",
            },
        )
        await self._session.commit()
        return ModuleDemandAssignResponse(
            assigned_count=int(result["assigned_count"]),
            assignment_ids=[str(x) for x in result["assignment_ids"]],
        )

    async def _aggregate_buckets(
        self,
        *,
        tenant_id: UUID | None,
        chatbot_period_days: int,
    ) -> list[_DemandBucket]:
        modules = await self._load_matchable_modules(tenant_id=tenant_id)
        by_id: dict[UUID, Module] = {m.id: m for m in modules}
        title_index: dict[str, Module] = {}
        for module in modules:
            for alias in _title_aliases(module.title_localized):
                existing = title_index.get(alias)
                if existing is None:
                    title_index[alias] = module
                elif existing.lifecycle_status != "published" and module.lifecycle_status == "published":
                    title_index[alias] = module

        buckets: dict[str, _DemandBucket] = {}

        rows = await self._requests.list_all(tenant_id=tenant_id)
        for row in rows:
            module: Module | None = None
            free_name: str | None = None

            if row.module_id is not None:
                module = by_id.get(row.module_id)
                if module is None:
                    module = await self._session.get(Module, row.module_id)
                    if (
                        module is not None
                        and tenant_id is not None
                        and module.tenant_id is not None
                        and module.tenant_id != tenant_id
                    ):
                        module = None
            elif row.requested_module_name and row.requested_module_name.strip():
                free_name = row.requested_module_name.strip()
                module = title_index.get(_normalize_name(free_name))

            if module is not None:
                self._add_to_bucket(
                    buckets,
                    module=module,
                    chw_id=row.chw_id,
                    free_name=free_name,
                )
            else:
                name = free_name or "Unknown request"
                key = f"name:{_normalize_name(name)}"
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = _DemandBucket(display_name=name)
                    buckets[key] = bucket
                bucket.chw_ids.add(row.chw_id)

        await self._merge_chatbot_demand(
            buckets,
            by_id=by_id,
            tenant_id=tenant_id,
            period_days=chatbot_period_days,
        )
        return list(buckets.values())

    async def _merge_chatbot_demand(
        self,
        buckets: dict[str, _DemandBucket],
        *,
        by_id: dict[UUID, Module],
        tenant_id: UUID | None,
        period_days: int,
    ) -> None:
        # Soft-fail ClickHouse: form demand still powers the summary.
        try:
            by_module = await self._chatbot.distinct_chw_by_module_id(
                tenant_id=tenant_id,
                period_days=period_days,
            )
        except Exception:
            logger.exception("Module demand: ClickHouse chatbot demand aggregation failed")
            return

        for module_id, chw_ids in by_module.items():
            module = by_id.get(module_id)
            if module is None:
                module = await self._session.get(Module, module_id)
                if (
                    module is not None
                    and tenant_id is not None
                    and module.tenant_id is not None
                    and module.tenant_id != tenant_id
                ):
                    module = None
            if module is None:
                continue
            for chw_id in chw_ids:
                self._add_to_bucket(buckets, module=module, chw_id=chw_id, free_name=None)

    def _add_to_bucket(
        self,
        buckets: dict[str, _DemandBucket],
        *,
        module: Module,
        chw_id: int,
        free_name: str | None,
    ) -> None:
        key = f"module:{module.id}"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = _DemandBucket(
                display_name=_display_title(module.title_localized),
                module_id=module.id,
                lifecycle_status=module.lifecycle_status,
                domain=module.domain,
                chatbot_faqs_only=bool(module.chatbot_faqs_only),
            )
            buckets[key] = bucket
        bucket.chw_ids.add(chw_id)
        if free_name:
            bucket.matched_names.add(_normalize_name(free_name))

    async def _load_matchable_modules(self, *, tenant_id: UUID | None) -> list[Module]:
        stmt = select(Module).where(Module.lifecycle_status.in_(["draft", "published"]))
        if tenant_id is not None:
            stmt = stmt.where((Module.tenant_id.is_(None)) | (Module.tenant_id == tenant_id))
        return list((await self._session.execute(stmt)).scalars().all())

    def _to_item(self, bucket: _DemandBucket) -> ModuleDemandItem:
        if bucket.module_id is None:
            return ModuleDemandItem(
                display_name=bucket.display_name,
                request_count=bucket.request_count,
                module_id=None,
                lifecycle_status=None,
                domain=None,
                action=ModuleDemandAction.CREATE,
                domain_filter=None,
            )

        status = bucket.lifecycle_status or ""
        if status == "published" and not bucket.chatbot_faqs_only:
            return ModuleDemandItem(
                display_name=bucket.display_name,
                request_count=bucket.request_count,
                module_id=bucket.module_id,
                lifecycle_status=status,
                domain=bucket.domain,
                action=ModuleDemandAction.ASSIGN,
                domain_filter=None,
            )
        if status == "draft":
            return ModuleDemandItem(
                display_name=bucket.display_name,
                request_count=bucket.request_count,
                module_id=bucket.module_id,
                lifecycle_status=status,
                domain=bucket.domain,
                action=ModuleDemandAction.OPEN_DRAFT,
                domain_filter=bucket.domain,
            )
        return ModuleDemandItem(
            display_name=bucket.display_name,
            request_count=bucket.request_count,
            module_id=bucket.module_id,
            lifecycle_status=status or None,
            domain=bucket.domain,
            action=ModuleDemandAction.CREATE,
            domain_filter=None,
        )

    async def _generate_llm_summary(
        self,
        *,
        ranked: list[_DemandBucket],
        top_k: int,
        available_count: int,
        unavailable_count: int,
        llm_timeout_seconds: float | None = None,
    ) -> str:
        timeout_seconds = (
            llm_timeout_seconds if llm_timeout_seconds is not None else MODULE_DEMAND_LLM_TIMEOUT_SECONDS
        )
        soft = fallback_summary(
            available_count=available_count,
            unavailable_count=unavailable_count,
            top_k=top_k,
        )
        if not ranked:
            return soft

        lines: list[str] = []
        for bucket in ranked:
            item = self._to_item(bucket)
            category = (
                "ready to assign"
                if item.action == ModuleDemandAction.ASSIGN
                else "draft — needs publish"
                if item.action == ModuleDemandAction.OPEN_DRAFT
                else "not in catalog — needs creation"
            )
            lines.append(f"- {item.display_name}: {item.request_count} distinct requestors ({category})")

        rendered = await PromptTemplateService().render(
            self._session,
            template_id=MODULE_DEMAND_TEMPLATE_ID,
            variant_key=None,
            variables=build_module_demand_variables(demand_lines=lines, top_k=top_k),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_DEMAND_SUMMARY,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="text"),
            trace_context=TraceContext(),
        )
        try:
            response = await asyncio.wait_for(
                self._client.generate(request),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Module demand summary: ai-runtime timed out after %.1fs",
                timeout_seconds,
            )
            return soft
        except Exception:
            logger.exception("Module demand summary: ai-runtime call failed")
            return soft

        if response.error:
            logger.warning("Module demand summary: ai-runtime error: %s", response.error)
            return soft

        text = (response.raw_text or "").strip()
        if not text:
            parsed: Any = response.parsed_json
            if isinstance(parsed, dict):
                for key in ("summary", "llm_summary", "text"):
                    value = parsed.get(key)
                    if isinstance(value, str) and value.strip():
                        text = value.strip()
                        break
        return text or soft

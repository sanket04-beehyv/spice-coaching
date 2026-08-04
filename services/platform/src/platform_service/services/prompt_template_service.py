"""Load, cache, validate, and render DB-backed prompt templates."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from mc_contracts.internal_ai import PromptSpec
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.base import SessionLocal
from platform_service.db.models.prompt_template import PromptTemplate
from platform_service.db.repositories.prompt_template_repository import PromptTemplateRepository

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class PromptTemplateError(Exception):
    """Raised when a prompt template cannot be loaded or rendered."""


class PromptTemplateRenderError(PromptTemplateError):
    """Raised when template substitution fails."""


@dataclass(frozen=True)
class RenderedPrompt:
    template_id: str
    template_version: int
    prompt_template_id: UUID
    resolved_system_prompt: str
    resolved_human_message: str


class _StrictFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        raise PromptTemplateRenderError(f"missing template variable: {key}")


class PromptTemplateService:
    _cache: dict[tuple[str, str | None], PromptTemplate] = {}
    _cache_lock = Lock()

    @classmethod
    def invalidate_cache(
        cls,
        *,
        template_id: str | None = None,
        variant_key: str | None = None,
    ) -> None:
        with cls._cache_lock:
            if template_id is None:
                cls._cache.clear()
                return
            keys = [k for k in cls._cache if k[0] == template_id]
            if variant_key is not None:
                keys = [k for k in keys if k[1] == variant_key]
            for key in keys:
                cls._cache.pop(key, None)

    async def get_active(
        self,
        session: AsyncSession,
        template_id: str,
        *,
        variant_key: str | None = None,
    ) -> PromptTemplate:
        cache_key = (template_id, variant_key)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        repo = PromptTemplateRepository(session)
        row = await repo.get_active(template_id, variant_key=variant_key)
        if row is None:
            raise PromptTemplateError(
                f"no active prompt template for template_id={template_id!r} variant_key={variant_key!r}"
            )
        with self._cache_lock:
            self._cache[cache_key] = row
        return row

    @staticmethod
    def extract_placeholders(template: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for match in _PLACEHOLDER_RE.finditer(template):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                ordered.append(name)
        return ordered

    @staticmethod
    def validate_variables(
        *,
        required_variables: list[str],
        variables: dict[str, str],
        template_id: str,
    ) -> None:
        missing = [name for name in required_variables if name not in variables]
        if missing:
            raise PromptTemplateRenderError(f"prompt {template_id!r} missing variables: {', '.join(missing)}")

    @staticmethod
    def substitute(template: str, variables: dict[str, str]) -> str:
        try:
            return template.format_map(_StrictFormatDict(variables))
        except KeyError as exc:
            raise PromptTemplateRenderError(f"missing template variable: {exc.args[0]}") from exc

    @staticmethod
    def validate_template_syntax(
        *,
        system_prompt_template: str,
        human_message_template: str,
        required_variables: list[str],
    ) -> None:
        for template in (system_prompt_template, human_message_template):
            placeholders = PromptTemplateService.extract_placeholders(template)
            for name in placeholders:
                if name not in required_variables:
                    raise PromptTemplateRenderError(
                        f"template placeholder {name!r} not listed in required_variables"
                    )

    async def render(
        self,
        session: AsyncSession | None,
        *,
        template_id: str,
        variant_key: str | None,
        variables: dict[str, str],
    ) -> RenderedPrompt:
        if session is None:
            async with SessionLocal() as owned:
                return await self._render(
                    owned, template_id=template_id, variant_key=variant_key, variables=variables
                )
        return await self._render(
            session, template_id=template_id, variant_key=variant_key, variables=variables
        )

    async def _render(
        self,
        session: AsyncSession,
        *,
        template_id: str,
        variant_key: str | None,
        variables: dict[str, str],
    ) -> RenderedPrompt:
        row = await self.get_active(session, template_id, variant_key=variant_key)
        self.validate_variables(
            required_variables=list(row.required_variables or []),
            variables=variables,
            template_id=template_id,
        )
        system = self.substitute(row.system_prompt_template, variables)
        human = self.substitute(row.human_message_template, variables)
        return RenderedPrompt(
            template_id=row.template_id,
            template_version=row.version,
            prompt_template_id=row.id,
            resolved_system_prompt=system,
            resolved_human_message=human,
        )

    async def preview(
        self,
        session: AsyncSession,
        *,
        template_id: str,
        variant_key: str | None,
        version: int | None,
        variables: dict[str, str],
    ) -> RenderedPrompt:
        repo = PromptTemplateRepository(session)
        if version is None:
            row = await repo.get_active(template_id, variant_key=variant_key)
        else:
            row = await repo.get_version(template_id, version, variant_key=variant_key)
        if row is None:
            raise PromptTemplateError(
                f"prompt template not found for template_id={template_id!r} "
                f"variant_key={variant_key!r} version={version!r}"
            )
        self.validate_variables(
            required_variables=list(row.required_variables or []),
            variables=variables,
            template_id=template_id,
        )
        return RenderedPrompt(
            template_id=row.template_id,
            template_version=row.version,
            prompt_template_id=row.id,
            resolved_system_prompt=self.substitute(row.system_prompt_template, variables),
            resolved_human_message=self.substitute(row.human_message_template, variables),
        )


def prompt_spec_from_rendered(rendered: RenderedPrompt) -> PromptSpec:
    return PromptSpec(
        template_id=rendered.template_id,
        template_version=rendered.template_version,
        resolved_system_prompt=rendered.resolved_system_prompt,
        resolved_human_message=rendered.resolved_human_message,
        prompt_template_db_id=rendered.prompt_template_id,
    )

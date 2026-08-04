"""Repository for DB-backed prompt templates."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.prompt_template import PromptTemplate


class PromptTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _variant_filter(self, variant_key: str | None):
        if variant_key is None:
            return PromptTemplate.variant_key.is_(None)
        return PromptTemplate.variant_key == variant_key

    async def get_by_id(self, prompt_id: UUID) -> PromptTemplate | None:
        return await self._session.get(PromptTemplate, prompt_id)

    async def get_active(
        self,
        template_id: str,
        *,
        variant_key: str | None = None,
    ) -> PromptTemplate | None:
        stmt = select(PromptTemplate).where(
            PromptTemplate.template_id == template_id,
            self._variant_filter(variant_key),
            PromptTemplate.status == "active",
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_version(
        self,
        template_id: str,
        version: int,
        *,
        variant_key: str | None = None,
    ) -> PromptTemplate | None:
        stmt = select(PromptTemplate).where(
            PromptTemplate.template_id == template_id,
            PromptTemplate.version == version,
            self._variant_filter(variant_key),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_versions(
        self,
        template_id: str,
        *,
        variant_key: str | None = None,
    ) -> list[PromptTemplate]:
        stmt = (
            select(PromptTemplate)
            .where(
                PromptTemplate.template_id == template_id,
                self._variant_filter(variant_key),
            )
            .order_by(PromptTemplate.version.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_catalog(self) -> list[PromptTemplate]:
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.status == "active")
            .order_by(PromptTemplate.template_id, PromptTemplate.variant_key.nullsfirst())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_next_version(
        self,
        template_id: str,
        *,
        variant_key: str | None = None,
    ) -> int:
        stmt = select(func.coalesce(func.max(PromptTemplate.version), 0)).where(
            PromptTemplate.template_id == template_id,
            self._variant_filter(variant_key),
        )
        current = (await self._session.execute(stmt)).scalar_one()
        return int(current) + 1

    async def create_version(
        self,
        *,
        template_id: str,
        version: int,
        variant_key: str | None,
        generation_type: str,
        system_prompt_template: str,
        human_message_template: str,
        required_variables: list[str],
        title: str | None = None,
        description: str | None = None,
        change_notes: str | None = None,
        status: str = "deprecated",
    ) -> PromptTemplate:
        row = PromptTemplate(
            template_id=template_id,
            version=version,
            variant_key=variant_key,
            generation_type=generation_type,
            system_prompt_template=system_prompt_template,
            human_message_template=human_message_template,
            required_variables=required_variables,
            title=title,
            description=description,
            change_notes=change_notes,
            status=status,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def activate_version(self, row: PromptTemplate) -> PromptTemplate:
        """Deprecate the current active row and activate the target version."""
        await self._session.execute(
            update(PromptTemplate)
            .where(
                PromptTemplate.template_id == row.template_id,
                self._variant_filter(row.variant_key),
                PromptTemplate.status == "active",
                PromptTemplate.id != row.id,
            )
            .values(status="deprecated", updated_at=datetime.now(UTC))
        )
        row.status = "active"
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def seed_row_if_absent(
        self,
        *,
        row_id: UUID,
        template_id: str,
        version: int,
        variant_key: str | None,
        generation_type: str,
        system_prompt_template: str,
        human_message_template: str,
        required_variables: list[str],
        title: str | None = None,
        description: str | None = None,
        change_notes: str | None = None,
    ) -> PromptTemplate | None:
        existing = await self.get_version(template_id, version, variant_key=variant_key)
        if existing is not None:
            return None
        row = PromptTemplate(
            id=row_id,
            template_id=template_id,
            version=version,
            variant_key=variant_key,
            generation_type=generation_type,
            system_prompt_template=system_prompt_template,
            human_message_template=human_message_template,
            required_variables=required_variables,
            title=title,
            description=description,
            change_notes=change_notes,
            status="active",
        )
        self._session.add(row)
        await self._session.flush()
        return row

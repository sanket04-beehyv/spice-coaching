"""Admin prompt template endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from mc_contracts.admin_prompts import (
    PromptTemplateActivateRequest,
    PromptTemplateCatalogEntry,
    PromptTemplateCreateVersionRequest,
    PromptTemplatePreviewRequest,
    PromptTemplatePreviewResponse,
    PromptTemplateResponse,
    PromptTemplateVariablesResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.prompt_template import PromptTemplate
from platform_service.db.repositories.prompt_template_repository import PromptTemplateRepository
from platform_service.deps import get_db
from platform_service.services.prompt_registry import PROMPT_REGISTRY
from platform_service.services.prompt_template_service import (
    PromptTemplateError,
    PromptTemplateRenderError,
    PromptTemplateService,
)

router = APIRouter(prefix="/admin", tags=["admin-prompts"])


def _to_response(row: PromptTemplate) -> PromptTemplateResponse:
    return PromptTemplateResponse.model_validate(row)


def _catalog_entry(row: PromptTemplate) -> PromptTemplateCatalogEntry:
    return PromptTemplateCatalogEntry(
        template_id=row.template_id,
        variant_key=row.variant_key,
        active_version=row.version,
        generation_type=row.generation_type,
        title=row.title,
        description=row.description,
        required_variables=list(row.required_variables or []),
        updated_at=row.updated_at,
    )


@router.get("/prompts", response_model=list[PromptTemplateCatalogEntry])
async def list_prompt_catalog(
    session: AsyncSession = Depends(get_db),
) -> list[PromptTemplateCatalogEntry]:
    repo = PromptTemplateRepository(session)
    rows = await repo.list_catalog()
    return [_catalog_entry(row) for row in rows]


@router.get("/prompts/{template_id}", response_model=list[PromptTemplateResponse])
async def list_prompt_versions(
    template_id: str,
    variant_key: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> list[PromptTemplateResponse]:
    repo = PromptTemplateRepository(session)
    rows = await repo.list_versions(template_id, variant_key=variant_key)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Prompt template '{template_id}' not found.")
    return [_to_response(row) for row in rows]


@router.get("/prompts/{template_id}/versions/{version}", response_model=PromptTemplateResponse)
async def get_prompt_version(
    template_id: str,
    version: int,
    variant_key: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    repo = PromptTemplateRepository(session)
    row = await repo.get_version(template_id, version, variant_key=variant_key)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt template '{template_id}' version {version} not found.",
        )
    return _to_response(row)


@router.get("/prompts/{template_id}/variables", response_model=PromptTemplateVariablesResponse)
async def get_prompt_variables(
    template_id: str,
    variant_key: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> PromptTemplateVariablesResponse:
    repo = PromptTemplateRepository(session)
    row = await repo.get_active(template_id, variant_key=variant_key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Active prompt template '{template_id}' not found.")
    return PromptTemplateVariablesResponse(
        template_id=row.template_id,
        variant_key=row.variant_key,
        active_version=row.version,
        required_variables=list(row.required_variables or []),
    )


@router.post("/prompts/{template_id}/versions", response_model=PromptTemplateResponse)
async def create_prompt_version(
    template_id: str,
    body: PromptTemplateCreateVersionRequest,
    session: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    generation_type = PROMPT_REGISTRY.get(template_id)
    if generation_type is None:
        repo = PromptTemplateRepository(session)
        existing = await repo.list_versions(template_id, variant_key=body.variant_key)
        if not existing:
            raise HTTPException(status_code=404, detail=f"Unknown prompt template '{template_id}'.")
        generation_type_value = existing[0].generation_type
    else:
        generation_type_value = generation_type.value

    try:
        PromptTemplateService.validate_template_syntax(
            system_prompt_template=body.system_prompt_template,
            human_message_template=body.human_message_template,
            required_variables=body.required_variables,
        )
    except PromptTemplateRenderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    repo = PromptTemplateRepository(session)
    next_version = await repo.get_next_version(template_id, variant_key=body.variant_key)
    active = await repo.get_active(template_id, variant_key=body.variant_key)
    created = await repo.create_version(
        template_id=template_id,
        version=next_version,
        variant_key=body.variant_key,
        generation_type=generation_type_value,
        system_prompt_template=body.system_prompt_template,
        human_message_template=body.human_message_template,
        required_variables=body.required_variables,
        title=body.title or (active.title if active else None),
        description=body.description or (active.description if active else None),
        change_notes=body.change_notes,
        status="deprecated",
    )
    await session.commit()
    await session.refresh(created)
    return _to_response(created)


@router.post("/prompts/{template_id}/versions/{version}/activate", response_model=PromptTemplateResponse)
async def activate_prompt_version(
    template_id: str,
    version: int,
    body: PromptTemplateActivateRequest,
    session: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    repo = PromptTemplateRepository(session)
    row = await repo.get_version(template_id, version, variant_key=body.variant_key)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt template '{template_id}' version {version} not found.",
        )
    activated = await repo.activate_version(row)
    await session.commit()
    await session.refresh(activated)
    PromptTemplateService.invalidate_cache(template_id=template_id, variant_key=body.variant_key)
    return _to_response(activated)


@router.post("/prompts/{template_id}/preview", response_model=PromptTemplatePreviewResponse)
async def preview_prompt(
    template_id: str,
    body: PromptTemplatePreviewRequest,
    session: AsyncSession = Depends(get_db),
) -> PromptTemplatePreviewResponse:
    service = PromptTemplateService()
    try:
        rendered = await service.preview(
            session,
            template_id=template_id,
            variant_key=body.variant_key,
            version=body.version,
            variables=body.variables,
        )
    except (PromptTemplateError, PromptTemplateRenderError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PromptTemplatePreviewResponse(
        template_id=rendered.template_id,
        template_version=rendered.template_version,
        prompt_template_id=rendered.prompt_template_id,
        resolved_system_prompt=rendered.resolved_system_prompt,
        resolved_human_message=rendered.resolved_human_message,
    )

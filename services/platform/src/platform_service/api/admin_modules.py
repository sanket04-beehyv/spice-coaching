"""Admin dashboard module endpoints.

Per `docs/ARCHITECTURE_RESET.md`. Trigger bindings live in
``admin_trigger_bindings``; ingestion run list/detail in ``admin_ingestion_runs``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from asyncpg import Range  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mc_contracts.admin_modules import (
    ClinicalFlagRequest,
    ModuleCreateRequest,
    ModuleDetail,
    ModuleEditRequest,
    ModuleSummary,
    SemanticSearchRequest,
    VisibilityWindowRequest,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import resolve_tenant_id_for_admin
from platform_service.celery_tasks import (
    bind_assessment_triggers_task,
    classify_module_gaps_task,
    generate_module_embedding_task,
    generate_module_quiz_task,
)
from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_gap_repository import (
    ModuleGapLinkError,
    ModuleGapRepository,
)
from platform_service.db.repositories.module_repository import (
    ModuleNotFoundError,
    ModuleRepository,
)
from platform_service.db.validators import ValidationError
from platform_service.deps import get_ai_client, get_db, get_object_storage_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.module_attachment_validator import validate_module_attachments
from platform_service.services.module_card_body_validator import validate_module_card_bodies
from platform_service.services.module_presenter import (
    cards_with_source_pages,
    get_quiz_counts,
    quiz_payload,
    source_documents_for_module,
    summary_from_module,
    visibility_window_bounds,
)
from platform_service.services.module_quiz_service import ModuleQuizService
from platform_service.services.module_thumbnail_service import validate_module_thumbnail_storage_path
from platform_service.services.object_storage import ObjectStorageClient

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])


@router.post("/modules", status_code=201)
async def create_new_module(
    body: ModuleCreateRequest,
    session: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Manually create a new module family and initial draft module version."""
    module_json = body.module_json
    if module_json is not None:
        try:
            module_json = validate_module_card_bodies(module_json)
            module_json = await validate_module_attachments(
                module_json,
                settings=settings,
                storage=storage,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

    repo = ModuleRepository(session)
    try:
        new_module = await repo.create_module(
            title=body.title,
            description=body.description,
            domain=body.domain,
            sub_domain=body.sub_domain,
            module_type=body.module_type,
            estimated_minutes=body.estimated_minutes,
            difficulty_level=body.difficulty_level,
            module_json=module_json,
            creator_id=body.creator_id,
            behavioural_gap_ids=body.behavioural_gap_ids,
            primary_gap_id=body.primary_gap_id,
        )
    except ModuleGapLinkError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    quiz_data = body.quiz
    if quiz_data is None and body.module_json is not None:
        quiz_data = body.module_json.get("quiz")

    if quiz_data is not None:
        await ModuleQuizService(session).append_questions(new_module.id, quiz_data)

    await session.commit()
    return {
        "id": str(new_module.id),
        "module_family_id": str(new_module.module_family_id),
        "version": new_module.version,
    }


@router.get("/modules", response_model=list[ModuleSummary])
async def list_modules(
    request: Request,
    status: str | None = Query(None, description="draft | published | retired"),
    clinically_reviewed: bool | None = Query(None),
    has_visibility_window: bool | None = Query(None),
    has_quality_flags: bool | None = Query(
        None,
        description="true → only modules with non-empty quality_flags_jsonb (the 'needs attention' view)",
    ),
    domain: str | None = Query(None),
    q: str | None = Query(None, description="full-text query against title + description"),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    latest_version_only: bool = Query(
        True,
        description="When true (default), collapse to one row per module_family showing the highest-version row that matches filters. Set false to see every version.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> list[ModuleSummary]:
    effective_tenant = resolve_tenant_id_for_admin(request, tenant_id)
    repo = ModuleRepository(session)
    modules = await repo.list_modules(
        status=status,
        clinically_reviewed=clinically_reviewed,
        has_visibility_window=has_visibility_window,
        has_quality_flags=has_quality_flags,
        domain=domain,
        full_text_query=q,
        latest_version_only=latest_version_only,
        tenant_id=effective_tenant,
        limit=limit,
        offset=offset,
    )
    quiz_counts = await get_quiz_counts(session, [m.id for m in modules])
    return [
        await summary_from_module(
            m,
            card_count=len((m.module_json or {}).get("cards", [])),
            quiz_count=quiz_counts.get(m.id, 0),
            storage=storage,
        )
        for m in modules
    ]


@router.get("/modules/{module_id}", response_model=ModuleDetail)
async def get_module(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> ModuleDetail:
    repo = ModuleRepository(session)
    module = await repo.get_module(module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="module not found")
    quiz = await repo.list_quiz_questions(module_id)
    module_payload = module.module_json or {}
    source_documents = await source_documents_for_module(session, module, storage)
    presigned_by_doc = {ref.source_document_id: ref.presigned_url for ref in source_documents}
    presigned_expires_by_doc = {
        ref.source_document_id: ref.presigned_expires_seconds for ref in source_documents
    }
    cards = await cards_with_source_pages(
        session,
        list(module_payload.get("cards", [])),
        storage=storage,
        presigned_by_doc=presigned_by_doc,
        presigned_expires_by_doc=presigned_expires_by_doc,
    )
    module_attachments = list(module_payload.get("attachments", []))
    summary = await summary_from_module(module, card_count=len(cards), quiz_count=len(quiz), storage=storage)
    window_lower, window_upper = visibility_window_bounds(module)
    gap_repo = ModuleGapRepository(session)
    behavioural_gap_ids = await gap_repo.get_gap_ids(module_id)
    return ModuleDetail(
        **summary.model_dump(),
        cards=cards,
        attachments=module_attachments,
        quiz=quiz_payload(quiz),
        sub_domain=module.sub_domain,
        difficulty_level=module.difficulty_level,
        pass_threshold_override=module.pass_threshold_override,
        visibility_window_lower=window_lower,
        visibility_window_upper=window_upper,
        source_documents=source_documents,
        primary_gap_id=module.primary_gap_id,
        behavioural_gap_ids=behavioural_gap_ids,
    )


@router.put("/modules/{module_id}")
async def edit_module(
    module_id: UUID,
    body: ModuleEditRequest,
    session: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    repo = ModuleRepository(session)
    module_json = body.module_json
    if module_json is not None:
        try:
            module_json = validate_module_card_bodies(module_json)
            module_json = await validate_module_attachments(
                module_json,
                settings=settings,
                storage=storage,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

    thumbnail_kw: dict[str, str | None] = {}
    if "thumbnail_storage_path" in body.model_fields_set:
        try:
            thumbnail_kw["thumbnail_storage_path"] = await validate_module_thumbnail_storage_path(
                body.thumbnail_storage_path,
                settings=settings,
                storage=storage,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc

    try:
        new_module = await repo.edit_module(
            module_id,
            title=body.title,
            description=body.description,
            module_json=module_json,
            editor_id=body.editor_id,
            **thumbnail_kw,
        )

        if body.behavioural_gap_ids is not None:
            gap_repo = ModuleGapRepository(session)
            primary = body.primary_gap_id
            if primary is None and body.behavioural_gap_ids:
                current = await session.get(Module, module_id)
                primary = current.primary_gap_id if current is not None else None
                if primary is None or primary not in body.behavioural_gap_ids:
                    primary = body.behavioural_gap_ids[0]
            try:
                await gap_repo.replace_links(
                    new_module.id,
                    gap_ids=body.behavioural_gap_ids,
                    primary_gap_id=primary if body.behavioural_gap_ids else None,
                )
            except ModuleGapLinkError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc

        quiz_data = body.quiz
        if quiz_data is None and body.module_json is not None:
            quiz_data = body.module_json.get("quiz")
        if quiz_data is not None:
            await ModuleQuizService(session).append_questions(new_module.id, quiz_data)

    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": str(new_module.id),
        "module_family_id": str(new_module.module_family_id),
        "version": new_module.version,
        "supersedes_module_id": str(new_module.supersedes_module_id)
        if new_module.supersedes_module_id
        else None,
    }


@router.post("/modules/{module_id}/clinically-reviewed")
async def set_clinically_reviewed(
    module_id: UUID,
    body: ClinicalFlagRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    repo = ModuleRepository(session)
    try:
        module = await repo.set_clinically_reviewed(
            module_id, flag=body.clinically_reviewed, reviewer_id=body.reviewer_id
        )
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": str(module.id),
        "clinically_reviewed": module.clinically_reviewed,
        "clinically_reviewed_at": module.clinically_reviewed_at,
        "clinically_reviewed_by": str(module.clinically_reviewed_by)
        if module.clinically_reviewed_by
        else None,
    }


@router.post("/modules/{module_id}/visibility-window")
async def set_visibility_window(
    module_id: UUID,
    body: VisibilityWindowRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    repo = ModuleRepository(session)
    window: Range | None
    if body.starts_at is None and body.ends_at is None:
        window = None
    else:
        window = Range(body.starts_at, body.ends_at, lower_inc=True, upper_inc=False)
    try:
        module = await repo.set_visibility_window(module_id, window=window)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": str(module.id),
        "visibility_window": (
            None
            if module.visibility_window is None
            else {
                "lower": getattr(module.visibility_window, "lower", None),
                "upper": getattr(module.visibility_window, "upper", None),
            }
        ),
    }


@router.delete("/modules/{module_id}")
async def retire_module(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    repo = ModuleRepository(session)
    try:
        module = await repo.retire_module(module_id)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "id": str(module.id),
        "lifecycle_status": module.lifecycle_status,
        "deprecated_at": module.deprecated_at,
    }


@router.post("/modules/search", response_model=list[ModuleSummary])
async def semantic_search(
    request: Request,
    body: SemanticSearchRequest,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
    ai_client: AIRuntimeClient = Depends(get_ai_client),
) -> list[ModuleSummary]:
    if body.query is None and body.query_vector is None:
        raise HTTPException(status_code=400, detail="provide either `query` or `query_vector`")
    if body.query_vector is not None:
        vec = body.query_vector
    else:
        vectors = await ai_client.embed([body.query or ""])
        if not vectors:
            raise HTTPException(status_code=502, detail="ai-runtime returned no embedding")
        vec = vectors[0]
    effective_tenant = resolve_tenant_id_for_admin(request, tenant_id)
    repo = ModuleRepository(session)
    pairs = await repo.search_by_embedding(
        query_vector=vec,
        limit=body.limit,
        tenant_id=effective_tenant,
    )
    modules = [m for m, _distance in pairs]
    quiz_counts = await get_quiz_counts(session, [m.id for m in modules])
    return [
        await summary_from_module(
            m,
            card_count=len((m.module_json or {}).get("cards", [])),
            quiz_count=quiz_counts.get(m.id, 0),
            storage=storage,
        )
        for m in modules
    ]


@router.post("/modules/{module_id}/regenerate-quiz")
async def regenerate_quiz(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if await session.get(Module, module_id) is None:
        raise HTTPException(status_code=404, detail="module not found")
    generate_module_quiz_task.delay(str(module_id))
    return {"id": str(module_id), "enqueued": "platform.generate_module_quiz"}


@router.post("/modules/{module_id}/regenerate-embedding")
async def regenerate_embedding(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if await session.get(Module, module_id) is None:
        raise HTTPException(status_code=404, detail="module not found")
    generate_module_embedding_task.delay(str(module_id))
    return {"id": str(module_id), "enqueued": "platform.generate_module_embedding"}


@router.post("/modules/{module_id}/regenerate-gap-classification")
async def regenerate_gap_classification(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if await session.get(Module, module_id) is None:
        raise HTTPException(status_code=404, detail="module not found")
    classify_module_gaps_task.delay(str(module_id))
    return {"id": str(module_id), "enqueued": "platform.classify_module_gaps"}


@router.post("/modules/{module_id}/bind-assessment-triggers")
async def bind_assessment_triggers(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if await session.get(Module, module_id) is None:
        raise HTTPException(status_code=404, detail="module not found")
    bind_assessment_triggers_task.delay(str(module_id))
    return {"id": str(module_id), "enqueued": "platform.bind_assessment_triggers"}

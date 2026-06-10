"""Build modules sync bundles for device sync."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import (
    ModuleFamilySyncPayload,
    ModuleQuizQuestionPayload,
    ModulesSyncBundle,
    ModuleSyncPayload,
    SourceDocumentSyncPayload,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.source_repository import SourceRepository


def build_source_document_sync_payloads(
    doc_ids: list[UUID],
    doc_by_id: dict[UUID, SourceDocument],
) -> list[SourceDocumentSyncPayload]:
    payloads: list[SourceDocumentSyncPayload] = []
    for doc_id in doc_ids:
        doc = doc_by_id.get(doc_id)
        if doc is None:
            continue
        payloads.append(
            SourceDocumentSyncPayload(
                source_document_id=doc.id,
                title=doc.title,
                source_type=doc.source_type,
                primary_language=doc.primary_language,
                content_domain=doc.content_domain,
                assessment_mode=doc.assessment_mode,
                authority_label=doc.authority_label,
                version_label=doc.version_label,
                publication_date=doc.publication_date,
                original_filename=doc.original_filename,
                has_thumbnail=bool(doc.thumbnail_storage_path),
            )
        )
    return payloads


class ModulesBundleBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(self, *, since: datetime, tenant_id: UUID | None = None) -> ModulesSyncBundle:
        module_repo = ModuleRepository(self._session)
        families = await module_repo.list_families_created_since(since, tenant_id=tenant_id)
        modules = await module_repo.list_published_modules_updated_since(since, tenant_id=tenant_id)

        quiz_by_module_id: dict[UUID, list[ModuleQuizQuestionPayload]] = {}
        module_ids = [module.id for module in modules]
        if module_ids:
            quiz_rows = await module_repo.list_quiz_questions_for_module_ids(module_ids)
            for row in quiz_rows:
                if row.module_id is None:
                    continue
                quiz_by_module_id.setdefault(row.module_id, []).append(
                    ModuleQuizQuestionPayload(
                        id=row.id,
                        question_order=row.question_order,
                        question_bn=row.question_bn,
                        question_en=row.question_en,
                        case_setup_bn=row.case_setup_bn,
                        case_setup_en=row.case_setup_en,
                        options_bn=list(row.options_bn or []),
                        options_en=list(row.options_en) if row.options_en else None,
                        correct_indices=list(row.correct_indices or []),
                        explanation_bn=row.explanation_bn,
                        explanation_en=row.explanation_en,
                        difficulty=row.difficulty,
                    )
                )

        all_doc_ids: list[UUID] = []
        seen_doc_ids: set[UUID] = set()
        for module in modules:
            for doc_id in module.source_document_ids or []:
                if doc_id not in seen_doc_ids:
                    seen_doc_ids.add(doc_id)
                    all_doc_ids.append(doc_id)

        doc_by_id: dict[UUID, SourceDocument] = {}
        if all_doc_ids:
            docs = await SourceRepository(self._session).list_source_documents_by_ids(all_doc_ids)
            doc_by_id = {doc.id: doc for doc in docs}

        payloads = []
        for module in modules:
            cards = list((module.module_json or {}).get("cards", []))
            doc_ids = list(module.source_document_ids or [])
            payloads.append(
                ModuleSyncPayload(
                    id=module.id,
                    module_family_id=module.module_family_id,
                    version=module.version,
                    title_bn=module.title_bn,
                    title_en=module.title_en,
                    description_bn=module.description_bn,
                    description_en=module.description_en,
                    domain=module.domain,
                    sub_domain=module.sub_domain,
                    module_type=module.module_type,
                    tenant_id=module.tenant_id,
                    estimated_minutes=module.estimated_minutes,
                    difficulty_level=module.difficulty_level,
                    pass_threshold_override=module.pass_threshold_override,
                    clinically_reviewed=module.clinically_reviewed,
                    published_at=module.published_at,
                    updated_at=module.updated_at,
                    source_documents=build_source_document_sync_payloads(doc_ids, doc_by_id),
                    has_thumbnail=bool(module.thumbnail_storage_path),
                    cards=cards,
                    quiz=list(quiz_by_module_id.get(module.id, [])),
                )
            )

        return ModulesSyncBundle(
            modules=payloads,
            module_families=[
                ModuleFamilySyncPayload(
                    id=family.id,
                    module_code=family.module_code,
                    created_at=family.created_at,
                    created_by=family.created_by,
                    current_published_module_id=family.current_published_module_id,
                )
                for family in families
            ],
            server_time_utc=datetime.now(UTC).isoformat(),
        )

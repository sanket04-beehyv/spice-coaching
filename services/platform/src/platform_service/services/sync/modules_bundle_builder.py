"""Build modules sync bundles for device sync."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mc_contracts.sync import (
    AssignedModulePayload,
    ModuleFamilySyncPayload,
    ModuleQuizQuestionPayload,
    ModulesSyncBundle,
    ModuleSyncPayload,
    RequestedModulePayload,
    SourceDocumentSyncPayload,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.db.repositories.training_request_repository import TrainingRequestRepository
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.card_provenance import (
    block_ids_from_card,
    render_card_provenance,
    resolve_card_provenance,
)
from platform_service.services.sync.module_assignment_resolver import resolve_assigned_modules


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

    async def build(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
        user_id: int | None = None,
        organization_ids: list[int] | None = None,
    ) -> ModulesSyncBundle:
        module_repo = ModuleRepository(self._session)
        families = await module_repo.list_families_created_since(since, tenant_id=tenant_id)
        modules = await module_repo.list_published_modules_updated_since(since, tenant_id=tenant_id)

        quiz_by_module_id: dict[UUID, list[ModuleQuizQuestionPayload]] = {}
        module_ids = [module.id for module in modules]
        cards_by_module_id: dict[UUID, list[dict[str, Any]]] = {}
        gap_ids_by_module: dict[UUID, list[UUID]] = {}
        if module_ids:
            gap_ids_by_module = await ModuleGapRepository(self._session).get_gap_ids_by_module_ids(module_ids)
            quiz_rows = await module_repo.list_quiz_questions_for_module_ids(module_ids)
            card_rows = await module_repo.list_cards_for_module_ids(module_ids)
            for row in card_rows:
                if row.module_id is None:
                    continue
                cards_by_module_id.setdefault(row.module_id, []).append(card_row_to_dict(row))
            for row in quiz_rows:
                if row.module_id is None:
                    continue
                quiz_by_module_id.setdefault(row.module_id, []).append(
                    ModuleQuizQuestionPayload(
                        id=row.id,
                        question_order=row.question_order,
                        question=row.question_localized,
                        case_setup=row.case_setup_localized,
                        options=row.options_localized,
                        correct_indices=list(row.correct_indices or []),
                        explanation=row.explanation_localized,
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

        module_cards = [(module, cards_by_module_id.get(module.id, [])) for module in modules]
        all_cards = [card for _, cards in module_cards for card in cards]
        provenance_context = await resolve_card_provenance(self._session, all_cards, storage=None)

        payloads = []
        for module, cards in module_cards:
            enriched_cards = []
            for card in cards:
                payload = dict(card)
                payload["source_pages"] = render_card_provenance(
                    block_ids_from_card(card),
                    provenance_context,
                )
                enriched_cards.append(payload)
            doc_ids = list(module.source_document_ids or [])
            payloads.append(
                ModuleSyncPayload(
                    id=module.id,
                    module_family_id=module.module_family_id,
                    version=module.version,
                    title=module.title_localized,
                    description=module.description_localized,
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
                    search_metadata=module.search_metadata_jsonb,
                    primary_gap_id=module.primary_gap_id,
                    behavioural_gap_ids=gap_ids_by_module.get(module.id, []),
                    cards=enriched_cards,
                    quiz=list(quiz_by_module_id.get(module.id, [])),
                )
            )

        assigned_module_ids: list[AssignedModulePayload] = []
        requested_modules: list[RequestedModulePayload] = []
        if user_id is not None:
            assignments_by_module = await resolve_assigned_modules(
                self._session,
                user_id=user_id,
                organization_ids=organization_ids,
            )
            for module_id in sorted(assignments_by_module):
                assigned_module_ids.append(
                    AssignedModulePayload(
                        module_id=module_id,
                        assigned_at=assignments_by_module[module_id],
                    )
                )
            request_rows = await TrainingRequestRepository(self._session).list_for_chw(
                chw_id=user_id,
                tenant_id=tenant_id,
            )
            requested_modules = [
                RequestedModulePayload(
                    request_id=row.id,
                    module_id=row.module_id,
                    requested_module_name=row.requested_module_name,
                    reason=row.reason,
                    submitted_at=row.submitted_at,
                )
                for row in request_rows
            ]

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
            assigned_module_ids=assigned_module_ids,
            requested_modules=requested_modules,
            server_time_utc=datetime.now(UTC).isoformat(),
        )

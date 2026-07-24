"""Coaching RAG retrieval, generation, and source attribution."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

from mc_contracts.coaching_rag import (
    CoachingRagRequest,
    CoachingRagResponse,
    RetrievedModuleHit,
    SourceAttribution,
    SourcePageRef,
)
from mc_contracts.enums import GenerationType, SourceDocumentType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    InferenceResponse,
    TraceContext,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.exceptions import EmbeddingDimensionError
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import (
    deployment_locales,
    migrate_legacy_card,
    primary_text,
)
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.coaching_rag_errors import CoachingRagError
from platform_service.services.embedding_vector import assert_embedding_dimension
from platform_service.services.llm_text_utils import strip_json_fence
from platform_service.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorageClient,
    ObjectStorageError,
    looks_like_object_storage_storage_path,
)
from platform_service.services.prompt_registry import COACHING_RAG_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.coaching_rag_variables import build_coaching_rag_variables

logger = logging.getLogger(__name__)

_KNOWN_SOURCE_TYPES = frozenset(e.value for e in SourceDocumentType)


def parse_rag_json(raw_text: str, parsed_json: Any) -> dict[str, Any]:
    if isinstance(parsed_json, dict):
        return parsed_json
    try:
        return json.loads(strip_json_fence(raw_text))
    except json.JSONDecodeError as exc:
        raise CoachingRagError(f"model returned non-JSON answer: {exc}") from exc


class CoachingRagService:
    def __init__(
        self,
        session: AsyncSession,
        ai: AIRuntimeClient,
        storage: ObjectStorageClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._ai = ai
        self._storage = storage
        self._settings = settings or get_settings()

    async def query(
        self,
        body: CoachingRagRequest,
        *,
        tenant_id: UUID | None = None,
    ) -> CoachingRagResponse:
        settings = self._settings
        ttl = min(
            settings.coaching_rag_presigned_url_ttl_seconds,
            settings.admin_file_presigned_max_seconds,
        )

        try:
            vectors = await self._ai.embed([body.question])
        except Exception:
            logger.exception("ai-runtime embed failed for rag-query")
            raise CoachingRagError("ai-runtime embed failed") from None
        if not vectors:
            raise CoachingRagError("ai-runtime returned no embedding for query")

        query_vec = self._assert_query_embedding(vectors[0], expected_dim=settings.embedding_dimension)
        pairs = await ModuleRepository(self._session).search_by_embedding(
            query_vector=query_vec,
            limit=settings.coaching_rag_module_limit,
            tenant_id=tenant_id,
        )
        if not pairs:
            raise CoachingRagError(
                "no published modules with embeddings matched the corpus; ingest/publish modules first",
                status_code=404,
            )

        per_mod = max(800, settings.coaching_rag_context_max_chars // max(1, len(pairs)))
        module_ids = [m.id for m, _ in pairs]
        card_rows = await ModuleRepository(self._session).list_cards_for_module_ids(module_ids)
        cards_by_module: dict[UUID, list[dict[str, Any]]] = {}
        for row in card_rows:
            if row.module_id is None:
                continue
            cards_by_module.setdefault(row.module_id, []).append(card_row_to_dict(row))
        context = self._build_retrieval_context(
            pairs,
            per_module_budget=per_mod,
            cards_by_module=cards_by_module,
        )
        resp = await self._generate_answer(body, context)
        if resp.error:
            raise CoachingRagError(f"ai-runtime error: {resp.error}")

        payload = parse_rag_json(resp.raw_text, resp.parsed_json)
        answer = (payload.get("answer") or "").strip()
        if not answer:
            raise CoachingRagError("model JSON missing non-empty 'answer' field")

        cited_ids = self._parse_cited_module_ids(payload.get("cited_module_ids") or [])
        suggested_questions = self._parse_suggested_questions(payload.get("suggested_questions"))
        retrieved_hits = [
            RetrievedModuleHit(
                module_id=m.id,
                title=m.title_localized,
                domain=m.domain,
                cosine_distance=dist,
            )
            for m, dist in pairs
        ]
        if not cited_ids:
            return CoachingRagResponse(
                answer=answer,
                retrieved_modules=retrieved_hits,
                source_documents=[],
                model=resp.model,
                cited_module_ids=[],
                suggested_questions=suggested_questions,
            )

        attributions = await self._build_attribution(
            cited_ids=cited_ids,
            ttl=ttl,
            cards_by_module=cards_by_module,
            tenant_id=tenant_id,
        )
        return CoachingRagResponse(
            answer=answer,
            retrieved_modules=retrieved_hits,
            source_documents=attributions,
            model=resp.model,
            cited_module_ids=cited_ids,
            suggested_questions=suggested_questions,
        )

    @staticmethod
    def _assert_query_embedding(vec: list[float], *, expected_dim: int) -> list[float]:
        try:
            return assert_embedding_dimension(vec, expected_dim=expected_dim)
        except EmbeddingDimensionError as exc:
            raise CoachingRagError(str(exc)) from exc

    def _cards_text_for_module(
        self,
        module: Module,
        cards: list[dict[str, Any]],
        budget_chars: int,
    ) -> str:
        primary_locale = deployment_locales(self._settings)
        lines: list[str] = []
        used = 0
        for i, card in enumerate(cards):
            if not isinstance(card, dict):
                continue
            migrated = migrate_legacy_card(dict(card), primary=primary_locale)
            title_map = migrated.get("title") if isinstance(migrated.get("title"), dict) else {}
            body_map = migrated.get("body") if isinstance(migrated.get("body"), dict) else {}
            title_primary = primary_text(title_map, settings=self._settings) or ""
            body_primary = card_body_plain_text(
                body_map.get(primary_locale) if isinstance(body_map, dict) else None
            )
            chunk = (
                f"--- card_index={i} ---\n"
                f"title[{primary_locale}]: {title_primary}\n"
                f"body[{primary_locale}]: {body_primary}\n"
            )
            if used + len(chunk) > budget_chars:
                lines.append(f"... truncated after card_index={i - 1} (char budget)")
                break
            lines.append(chunk)
            used += len(chunk)
        return "\n".join(lines)

    def _build_retrieval_context(
        self,
        pairs: list[tuple[Module, float]],
        *,
        per_module_budget: int,
        cards_by_module: dict[UUID, list[dict[str, Any]]],
    ) -> str:
        primary_locale = deployment_locales(self._settings)
        blocks: list[str] = []
        for mod, dist in pairs:
            cards_blob = self._cards_text_for_module(
                mod,
                cards_by_module.get(mod.id, []),
                per_module_budget,
            )
            title_primary = primary_text(mod.title_localized, settings=self._settings) or ""
            blocks.append(
                f"[[[ MODULE_BLOCK module_id={mod.id} cosine_distance={dist:.6f} ]]]\n"
                f"title[{primary_locale}]: {title_primary}\n"
                f"domain: {mod.domain}\n"
                f"CARD_CONTENT:\n{cards_blob}\n"
            )
        text = "\n\n".join(blocks)
        context_max_chars = self._settings.coaching_rag_context_max_chars
        if len(text) > context_max_chars:
            return text[:context_max_chars] + "\n... CONTEXT TRUNCATED ..."
        return text

    async def _generate_answer(self, body: CoachingRagRequest, context: str) -> InferenceResponse:
        settings = self._settings
        lang = body.response_language.strip() or settings.deployment_primary_locale
        supported = settings.deployment_locale_config.supported
        if lang not in supported:
            raise CoachingRagError(
                f"response_language must be one of {supported!r}, got {lang!r}",
                status_code=400,
            )
        rendered = await PromptTemplateService().render(
            self._session,
            template_id=COACHING_RAG_TEMPLATE_ID,
            variant_key=None,
            variables=build_coaching_rag_variables(
                question=body.question,
                context=context,
                lang=lang,
                settings=settings,
            ),
        )
        req = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.COACHING_RAG,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(
                language=lang,
                output_format="json",
            ),
            trace_context=TraceContext(),
            context={"question": body.question},
        )
        try:
            return await self._ai.generate(req)
        except Exception:
            logger.exception("ai-runtime generate failed for rag-query")
            raise CoachingRagError("ai-runtime generation failed") from None

    @staticmethod
    def _parse_suggested_questions(raw: Any, *, max_count: int = 5) -> list[str]:
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= max_count:
                break
        return out

    @staticmethod
    def _parse_cited_module_ids(cited_raw: list[Any]) -> list[UUID]:
        cited_ids: list[UUID] = []
        for item in cited_raw:
            try:
                cited_ids.append(UUID(str(item)))
            except (ValueError, TypeError):
                continue
        return cited_ids

    async def _build_attribution(
        self,
        *,
        cited_ids: list[UUID],
        ttl: int,
        cards_by_module: dict[UUID, list[dict[str, Any]]],
        tenant_id: UUID | None = None,
    ) -> list[SourceAttribution]:
        settings = self._settings
        module_repo = ModuleRepository(self._session)
        cited_modules = await module_repo.list_modules_by_ids(cited_ids, tenant_id=tenant_id)
        resolved_ids = {m.id for m in cited_modules}
        for mid in cited_ids:
            if mid not in resolved_ids:
                logger.warning("Cited module_id %s could not be resolved for attribution", mid)

        missing_card_ids = [m.id for m in cited_modules if m.id not in cards_by_module]
        if missing_card_ids:
            card_rows = await module_repo.list_cards_for_module_ids(missing_card_ids)
            for row in card_rows:
                if row.module_id is None:
                    continue
                cards_by_module.setdefault(row.module_id, []).append(card_row_to_dict(row))

        doc_id_set, module_ids_per_doc = self._collect_source_document_links(cited_modules)
        block_ids = self._block_ids_from_modules(cited_modules, cards_by_module)

        source_repo = SourceRepository(self._session)
        block_rows = await source_repo.list_block_provenance_by_ids(block_ids)
        pages_per_doc, page_refs_per_doc = self._provenance_per_document(block_rows)

        docs = await source_repo.list_source_documents_by_ids(list(doc_id_set))
        docs.sort(key=lambda d: (d.title.lower(), str(d.id)))

        bucket_name = settings.minio_bucket_name
        attributions: list[SourceAttribution] = []
        for doc in docs:
            if doc.source_type not in _KNOWN_SOURCE_TYPES:
                logger.warning(
                    "Skipping source_document %s with unknown source_type=%r",
                    doc.id,
                    doc.source_type,
                )
                continue

            storage_path = doc.original_storage_path
            object_name: str | None = None
            presigned: str | None = None

            if looks_like_object_storage_storage_path(storage_path, bucket_name=bucket_name):
                object_name = self._object_name_for_storage_path(storage_path)
                try:
                    p = await self._storage.presigned_get_url(
                        object_name=storage_path,
                        expires_seconds=ttl,
                        download_filename=doc.original_filename,
                    )
                    presigned = p.url
                    object_name = object_name or p.object_name
                except ObjectNotFoundError:
                    logger.warning(
                        "Presign: object missing for source_document %s path=%s",
                        doc.id,
                        storage_path,
                    )
                except (ObjectStorageError, ValueError) as exc:
                    logger.warning("Presign failed for source_document %s: %s", doc.id, exc)
            elif storage_path.startswith("/"):
                logger.debug(
                    "Skipping presign for legacy filesystem source_document %s path=%s",
                    doc.id,
                    storage_path,
                )

            attributions.append(
                SourceAttribution(
                    source_document_id=doc.id,
                    title=doc.title,
                    source_type=doc.source_type,  # type: ignore[arg-type]
                    storage_path=storage_path,
                    object_name=object_name,
                    original_filename=doc.original_filename,
                    content_sha256=doc.content_sha256,
                    page_numbers=pages_per_doc.get(doc.id, []),
                    source_pages=page_refs_per_doc.get(doc.id, []),
                    presigned_url=presigned,
                    presigned_expires_seconds=ttl if presigned else None,
                    linked_module_ids=sorted(set(module_ids_per_doc.get(doc.id, [])), key=str),
                )
            )
        return attributions

    @staticmethod
    def _collect_source_document_links(
        modules: list[Module],
    ) -> tuple[set[UUID], dict[UUID, list[UUID]]]:
        doc_id_set: set[UUID] = set()
        module_ids_per_doc: dict[UUID, list[UUID]] = {}
        for mod in modules:
            if not mod.source_document_ids:
                continue
            for did in mod.source_document_ids:
                doc_id_set.add(did)
                module_ids_per_doc.setdefault(did, []).append(mod.id)
        return doc_id_set, module_ids_per_doc

    @staticmethod
    def _block_ids_from_modules(
        modules: list[Module],
        cards_by_module: dict[UUID, list[dict[str, Any]]],
    ) -> list[UUID]:
        ids: list[UUID] = []
        for mod in modules:
            for card in cards_by_module.get(mod.id, []):
                if not isinstance(card, dict):
                    continue
                for raw in card.get("source_block_ids") or []:
                    try:
                        ids.append(UUID(str(raw)))
                    except (ValueError, TypeError):
                        continue
        return ids

    @staticmethod
    def _provenance_per_document(
        rows: list[tuple[UUID, int, UUID, int | None, int | None]],
    ) -> tuple[dict[UUID, list[int]], dict[UUID, list[SourcePageRef]]]:
        page_nums: dict[UUID, set[int]] = {}
        page_refs: dict[UUID, dict[int, SourcePageRef]] = {}
        for _block_id, page_number, doc_id, start_ms, end_ms in rows:
            page_nums.setdefault(doc_id, set()).add(page_number)
            refs = page_refs.setdefault(doc_id, {})
            if page_number not in refs:
                refs[page_number] = SourcePageRef(
                    page_number=page_number,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
        sorted_nums = {doc_id: sorted(nums) for doc_id, nums in page_nums.items()}
        sorted_refs = {
            doc_id: sorted(refs.values(), key=lambda r: r.page_number) for doc_id, refs in page_refs.items()
        }
        return sorted_nums, sorted_refs

    def _object_name_for_storage_path(self, storage_path: str) -> str | None:
        try:
            return self._storage.object_name_from_reference(storage_path)
        except ValueError:
            return None

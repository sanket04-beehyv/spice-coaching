"""Coaching RAG retrieval, generation, and source attribution."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import HTTPException
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
    ModelPolicy,
    PromptSpec,
    TraceContext,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.exceptions import EmbeddingDimensionError
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.embedding_vector import assert_embedding_dimension
from platform_service.services.llm_text_utils import strip_json_fence
from platform_service.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorageClient,
    ObjectStorageError,
    looks_like_object_storage_storage_path,
)

logger = logging.getLogger(__name__)

_CONTEXT_MAX_CHARS = 28_000
_KNOWN_SOURCE_TYPES = frozenset(e.value for e in SourceDocumentType)


def parse_rag_json(raw_text: str, parsed_json: Any) -> dict[str, Any]:
    if isinstance(parsed_json, dict):
        return parsed_json
    try:
        return json.loads(strip_json_fence(raw_text))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"model returned non-JSON answer: {exc}",
        ) from exc


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
        ttl = min(body.presigned_url_ttl_seconds, settings.admin_file_presigned_max_seconds)

        try:
            vectors = await self._ai.embed([body.question])
        except Exception:
            logger.exception("ai-runtime embed failed for rag-query")
            raise HTTPException(status_code=502, detail="ai-runtime embed failed") from None
        if not vectors:
            raise HTTPException(status_code=502, detail="ai-runtime returned no embedding for query")

        query_vec = self._assert_query_embedding(vectors[0], expected_dim=settings.embedding_dimension)
        pairs = await ModuleRepository(self._session).search_by_embedding(
            query_vector=query_vec,
            limit=body.module_limit,
            tenant_id=tenant_id,
        )
        if not pairs:
            raise HTTPException(
                status_code=404,
                detail="no published modules with embeddings matched the corpus; ingest/publish modules first",
            )

        per_mod = max(800, _CONTEXT_MAX_CHARS // max(1, len(pairs)))
        context = self._build_retrieval_context(pairs, per_module_budget=per_mod)
        resp = await self._generate_answer(body, context)
        if resp.error:
            raise HTTPException(status_code=502, detail=f"ai-runtime error: {resp.error}")

        payload = parse_rag_json(resp.raw_text, resp.parsed_json)
        answer = (payload.get("answer") or "").strip()
        if not answer:
            raise HTTPException(status_code=502, detail="model JSON missing non-empty 'answer' field")

        cited_ids = self._parse_cited_module_ids(payload.get("cited_module_ids") or [])
        retrieved_hits = [
            RetrievedModuleHit(
                module_id=m.id,
                title_bn=m.title_bn,
                title_en=m.title_en,
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
                model=resp.model or settings.text_model,
                cited_module_ids=[],
            )

        attributions = await self._build_attribution(
            pairs,
            cited_ids=cited_ids,
            ttl=ttl,
        )
        return CoachingRagResponse(
            answer=answer,
            retrieved_modules=retrieved_hits,
            source_documents=attributions,
            model=resp.model or settings.text_model,
            cited_module_ids=cited_ids,
        )

    @staticmethod
    def _assert_query_embedding(vec: list[float], *, expected_dim: int) -> list[float]:
        try:
            return assert_embedding_dimension(vec, expected_dim=expected_dim)
        except EmbeddingDimensionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @staticmethod
    def _cards_text_for_module(module: Module, budget_chars: int) -> str:
        mj = module.module_json or {}
        cards = mj.get("cards") or []
        lines: list[str] = []
        used = 0
        for i, card in enumerate(cards):
            if not isinstance(card, dict):
                continue
            title_bn = (card.get("title_bn") or "").strip()
            title_en = (card.get("title_en") or "").strip()
            body_bn = (card.get("body_bn") or "").strip()
            body_en = (card.get("body_en") or "").strip()
            chunk = (
                f"--- card_index={i} ---\n"
                f"title_bn: {title_bn}\n"
                f"title_en: {title_en}\n"
                f"body_bn: {body_bn}\n"
                f"body_en: {body_en}\n"
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
    ) -> str:
        blocks: list[str] = []
        for mod, dist in pairs:
            cards_blob = self._cards_text_for_module(mod, per_module_budget)
            blocks.append(
                f"[[[ MODULE_BLOCK module_id={mod.id} cosine_distance={dist:.6f} ]]]\n"
                f"title_bn: {mod.title_bn}\n"
                f"title_en: {mod.title_en or ''}\n"
                f"domain: {mod.domain}\n"
                f"CARD_CONTENT:\n{cards_blob}\n"
            )
        text = "\n\n".join(blocks)
        if len(text) > _CONTEXT_MAX_CHARS:
            return text[:_CONTEXT_MAX_CHARS] + "\n... CONTEXT TRUNCATED ..."
        return text

    async def _generate_answer(self, body: CoachingRagRequest, context: str) -> InferenceResponse:
        settings = self._settings
        lang = body.response_language
        system = (
            "You are a clinical / CHW training assistant. Answer ONLY using the MODULE_BLOCK excerpts. "
            "If the context is insufficient, say so explicitly. Respond with a single JSON object, no markdown fences, keys:\n"
            '- "answer": string (primary language: '
            f"{'Bangla (bn)' if lang == 'bn' else 'English (en)'}"
            ")\n"
            '- "cited_module_ids": array of UUID strings — only modules you relied on from the MODULE_BLOCK headers\n'
            '- "confidence": optional string "high"|"medium"|"low"\n'
        )
        human = (
            f"USER_QUESTION ({lang}):\n{body.question}\n\nRETRIEVAL_CONTEXT:\n{context}\nReturn JSON only."
        )
        req = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.COACHING_RAG,
            model_policy=ModelPolicy(model=settings.text_model),
            prompt=PromptSpec(
                template_id="coaching_rag_v1",
                template_version=1,
                resolved_system_prompt=system,
                resolved_human_message=human,
            ),
            constraints=GenerationConstraints(
                language=lang,
                output_format="json",
                max_tokens=2048,
                temperature=0.2,
            ),
            trace_context=TraceContext(),
            context={"question": body.question},
        )
        try:
            return await self._ai.generate(req)
        except Exception:
            logger.exception("ai-runtime generate failed for rag-query")
            raise HTTPException(status_code=502, detail="ai-runtime generation failed") from None

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
        pairs: list[tuple[Module, float]],
        *,
        cited_ids: list[UUID],
        ttl: int,
    ) -> list[SourceAttribution]:
        settings = self._settings
        modules_by_id = {m.id: m for m, _ in pairs}
        seed_ids = self._attribution_seed_module_ids(
            cited_ids=cited_ids,
            retrieved_module_ids=[m.id for m, _ in pairs],
        )
        doc_id_set, module_ids_per_doc = self._collect_source_document_links(
            pairs,
            link_filter_module_ids=set(cited_ids),
        )
        seed_modules = [modules_by_id[mid] for mid in seed_ids if mid in modules_by_id]
        block_ids = self._block_ids_from_modules(seed_modules)

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
    def _attribution_seed_module_ids(
        *,
        cited_ids: list[UUID],
        retrieved_module_ids: list[UUID],
    ) -> list[UUID]:
        return cited_ids if cited_ids else retrieved_module_ids

    @staticmethod
    def _collect_source_document_links(
        pairs: list[tuple[Module, float]],
        *,
        link_filter_module_ids: set[UUID] | None,
    ) -> tuple[set[UUID], dict[UUID, list[UUID]]]:
        doc_id_set: set[UUID] = set()
        module_ids_per_doc: dict[UUID, list[UUID]] = {}
        for mod, _dist in pairs:
            if not mod.source_document_ids:
                continue
            for did in mod.source_document_ids:
                doc_id_set.add(did)
                if link_filter_module_ids is not None and mod.id not in link_filter_module_ids:
                    continue
                module_ids_per_doc.setdefault(did, []).append(mod.id)
        return doc_id_set, module_ids_per_doc

    @staticmethod
    def _block_ids_from_modules(modules: list[Module]) -> list[UUID]:
        ids: list[UUID] = []
        for mod in modules:
            mj = mod.module_json or {}
            for card in mj.get("cards") or []:
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

"""Ingest upload orchestration — MinIO staging, provenance, source_document creation.

Owns the shared path for ``POST /admin/ingest`` (batch) and
``POST /admin/ingest/stream`` (single-file SSE). API routes validate HTTP
form params and enqueue Celery / SSE; this service handles bytes + DB rows.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from fastapi import UploadFile
from mc_contracts.enums import AssessmentMode, ContentDomain
from mc_contracts.internal_ai import GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES, OPENAI_TRANSCRIPTION_MAX_BYTES
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.file_upload_repository import FileUploadRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.db.tenant_scope import tenant_scope_filter
from platform_service.services.attribution_audit import record_attribution_event
from platform_service.services.file_digest import sha256_hex_file
from platform_service.services.ingest_errors import IngestValidationError
from platform_service.services.object_storage import (
    ObjectStorageClient,
    ObjectStorageError,
    StoredObject,
    safe_basename,
)
from platform_service.services.upload_provenance import (
    build_upload_metadata,
    record_file_upload,
)

logger = logging.getLogger(__name__)

_SOURCE_TYPE_BY_SUFFIX = {
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".webm": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
}
_ACCEPTED_SUFFIXES = frozenset(_SOURCE_TYPE_BY_SUFFIX)
_MEDIA_SOURCE_TYPES = frozenset({"audio", "video"})
_UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_INGEST_FILES = 10
_ALLOWED_CONTENT_DOMAINS = frozenset(e.value for e in ContentDomain)
_ALLOWED_ASSESSMENT_MODES = frozenset(e.value for e in AssessmentMode)


@dataclass(frozen=True)
class IngestedSourceResult:
    """One source_document created during admin ingest upload."""

    source_document_id: uuid.UUID
    title: str
    source_type: str
    stored_path: str


@dataclass(frozen=True)
class IngestUploadParams:
    content_domain: str
    assessment_mode: str
    authority_label: str
    primary_language: str
    uploaded_by: str
    retired_ids: list[uuid.UUID]
    ingestion_instructions: str | None = None


@dataclass(frozen=True)
class DuplicateIngestConflict:
    """One file skipped because matching content is already ingested."""

    filename: str
    title: str
    content_sha256: str
    existing_source_documents: tuple[SourceDocument, ...]


@dataclass(frozen=True)
class IngestUploadOutcome:
    """Result of attempting to ingest one uploaded file."""

    ingested: IngestedSourceResult | None = None
    skipped: DuplicateIngestConflict | None = None

    def __post_init__(self) -> None:
        if self.ingested is not None and self.skipped is not None:
            raise ValueError("ingested and skipped are mutually exclusive")
        if self.ingested is None and self.skipped is None:
            raise ValueError("one of ingested or skipped must be set")


class IngestUploadService:
    """Upload files to object storage and create source_document rows."""

    def __init__(
        self,
        db: AsyncSession,
        storage: ObjectStorageClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._storage = storage
        self._settings = settings or get_settings()

    @staticmethod
    def source_type_from_suffix(suffix: str) -> str:
        return _SOURCE_TYPE_BY_SUFFIX[suffix]

    @staticmethod
    def media_upload_limit_bytes(provider: str) -> int:
        """Return the strict provider inline transcription limit."""
        return (
            OPENAI_TRANSCRIPTION_MAX_BYTES if provider == "openai" else GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES
        )

    @staticmethod
    def validate_file_count(files: list[UploadFile]) -> None:
        if not files:
            raise IngestValidationError("at least one file is required")
        if len(files) > MAX_INGEST_FILES:
            raise IngestValidationError(
                f"at most {MAX_INGEST_FILES} files per request; got {len(files)}",
            )

    @staticmethod
    def validate_mode(mode: str) -> None:
        if mode not in ("append", "new"):
            raise IngestValidationError(f"invalid mode {mode!r}; must be 'append' or 'new'")

    @staticmethod
    def validate_ingest_metadata(*, content_domain: str, assessment_mode: str) -> None:
        if content_domain not in _ALLOWED_CONTENT_DOMAINS:
            raise IngestValidationError(
                f"invalid content_domain {content_domain!r}; "
                f"must be one of: {sorted(_ALLOWED_CONTENT_DOMAINS)}",
            )
        if assessment_mode not in _ALLOWED_ASSESSMENT_MODES:
            raise IngestValidationError(
                f"invalid assessment_mode {assessment_mode!r}; "
                f"must be one of: {sorted(_ALLOWED_ASSESSMENT_MODES)}",
            )

    @staticmethod
    def resolve_titles_for_files(titles_json: str | None, files: list[UploadFile]) -> list[str]:
        """Map each upload to a title: explicit JSON array or filename stem."""
        if not files:
            raise IngestValidationError("at least one file is required")
        if titles_json is None:
            resolved: list[str] = []
            for upload in files:
                if not upload.filename:
                    raise IngestValidationError("filename is required")
                stem = Path(safe_basename(upload.filename)).stem
                if not stem:
                    raise IngestValidationError(
                        f"cannot derive title from filename {upload.filename!r}",
                    )
                resolved.append(stem)
            return resolved
        try:
            parsed = json.loads(titles_json)
        except json.JSONDecodeError as exc:
            raise IngestValidationError("titles must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise IngestValidationError("titles must be a JSON array")
        if len(parsed) != len(files):
            raise IngestValidationError(
                f"titles must have {len(files)} entries (one per file); got {len(parsed)}",
            )
        resolved = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, str) or not entry.strip():
                raise IngestValidationError(
                    f"titles[{index}] must be a non-empty string",
                )
            resolved.append(entry.strip())
        return resolved

    @staticmethod
    def resolve_override_duplicates_for_files(
        override_json: str | None,
        files: list[UploadFile],
    ) -> list[bool]:
        """Map each upload to an override flag (default false when omitted)."""
        if not files:
            raise IngestValidationError("at least one file is required")
        if override_json is None:
            return [False] * len(files)
        try:
            parsed = json.loads(override_json)
        except json.JSONDecodeError as exc:
            raise IngestValidationError("override_duplicates must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise IngestValidationError("override_duplicates must be a JSON array")
        if len(parsed) != len(files):
            raise IngestValidationError(
                f"override_duplicates must have {len(files)} entries (one per file); got {len(parsed)}",
            )
        resolved: list[bool] = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, bool):
                raise IngestValidationError(
                    f"override_duplicates[{index}] must be a boolean",
                )
            resolved.append(entry)
        return resolved

    @staticmethod
    def resolve_sync_published_visible_for_files(
        visible_json: str | None,
        files: list[UploadFile],
    ) -> list[bool]:
        """Map each upload to a published-sync visibility flag (default false when omitted)."""
        if not files:
            raise IngestValidationError("at least one file is required")
        if visible_json is None:
            return [False] * len(files)
        try:
            parsed = json.loads(visible_json)
        except json.JSONDecodeError as exc:
            raise IngestValidationError("sync_published_visible must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise IngestValidationError("sync_published_visible must be a JSON array")
        if len(parsed) != len(files):
            raise IngestValidationError(
                f"sync_published_visible must have {len(files)} entries (one per file); got {len(parsed)}",
            )
        resolved: list[bool] = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, bool):
                raise IngestValidationError(
                    f"sync_published_visible[{index}] must be a boolean",
                )
            resolved.append(entry)
        return resolved

    @staticmethod
    def duplicate_conflict_payload(conflict: DuplicateIngestConflict) -> dict[str, Any]:
        return {
            "filename": conflict.filename,
            "title": conflict.title,
            "content_sha256": conflict.content_sha256,
            "existing_source_documents": [
                {
                    "source_document_id": str(doc.id),
                    "title": doc.title,
                    "original_filename": doc.original_filename,
                    "ingested_at": doc.ingested_at.isoformat(),
                    "status": doc.status,
                }
                for doc in conflict.existing_source_documents
            ],
        }

    @staticmethod
    def source_type_for_upload(file: UploadFile) -> str:
        if not file.filename:
            raise IngestValidationError("filename is required")
        suffix = Path(file.filename).suffix.lower()
        if suffix not in _ACCEPTED_SUFFIXES:
            raise IngestValidationError(
                f"unsupported file type {suffix!r}; accepted: {sorted(_ACCEPTED_SUFFIXES)}",
            )
        return IngestUploadService.source_type_from_suffix(suffix)

    async def retire_published_modules_if_new(
        self,
        mode: str,
        *,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[int, list[uuid.UUID]]:
        """Retire published modules for the active tenant when ``mode=='new'``."""
        if mode != "new":
            return 0, []
        stmt = (
            update(Module)
            .where(Module.lifecycle_status == "published")
            .values(lifecycle_status="retired")
            .returning(Module.id)
        )
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        result = await self._db.execute(stmt)
        retired_ids = list(result.scalars().all())
        logger.info(
            "Ingest mode=new: retired %d published module(s) before fresh ingestion (tenant_id=%s)",
            len(retired_ids),
            tenant_id,
        )
        return len(retired_ids), retired_ids

    async def ingest_uploaded_files(
        self,
        *,
        files: list[UploadFile],
        titles: list[str],
        params: IngestUploadParams,
        override_flags: list[bool],
        sync_published_visible_flags: list[bool],
    ) -> list[IngestUploadOutcome]:
        outcomes: list[IngestUploadOutcome] = []
        for upload, doc_title, override_duplicate, sync_published_visible in zip(
            files,
            titles,
            override_flags,
            sync_published_visible_flags,
            strict=True,
        ):
            outcomes.append(
                await self.ingest_one_uploaded_file(
                    file=upload,
                    title=doc_title,
                    params=params,
                    override_duplicate=override_duplicate,
                    sync_published_visible=sync_published_visible,
                )
            )
        return outcomes

    async def ingest_one_uploaded_file(
        self,
        *,
        file: UploadFile,
        title: str,
        params: IngestUploadParams,
        override_duplicate: bool = False,
        sync_published_visible: bool = False,
    ) -> IngestUploadOutcome:
        """Upload one file, persist provenance, and create a source_document."""
        self.validate_ingest_metadata(
            content_domain=params.content_domain,
            assessment_mode=params.assessment_mode,
        )
        source_type = self.source_type_for_upload(file)
        staging_path, content_sha256, original_filename = await self._stage_and_digest_upload(
            file,
            source_type=source_type,
        )
        source_repo = SourceRepository(self._db)
        try:
            existing = await source_repo.list_ingested_by_content_sha256(content_sha256)
            if existing and not override_duplicate:
                return IngestUploadOutcome(
                    skipped=DuplicateIngestConflict(
                        filename=original_filename,
                        title=title,
                        content_sha256=content_sha256,
                        existing_source_documents=tuple(existing),
                    )
                )

            try:
                stored = await self._put_staged_upload_to_object_storage(
                    staging_path,
                    original_filename=original_filename,
                )
            except ObjectStorageError:
                logger.exception("Ingest object storage upload failed for %s", file.filename)
                raise IngestValidationError("object storage upload failed", status_code=502) from None

            storage_path = stored.storage_path
            await record_file_upload(
                file_upload_repo=FileUploadRepository(self._db),
                bucket_name=stored.bucket_name,
                object_key=stored.object_name,
                storage_path=storage_path,
                original_filename=original_filename,
                content_sha256=content_sha256,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                uploaded_by=params.uploaded_by,
            )

            doc = await source_repo.create_source_document(
                title=title,
                source_type=source_type,
                primary_language=params.primary_language,
                content_domain=params.content_domain,
                assessment_mode=params.assessment_mode,
                authority_label=params.authority_label,
                original_storage_path=storage_path,
                content_sha256=content_sha256,
                original_filename=original_filename,
                uploaded_by=params.uploaded_by,
                ingestion_instructions=params.ingestion_instructions,
                sync_published_visible=sync_published_visible,
            )
            audit_payload: dict[str, Any] = {
                "stored_path": storage_path,
                "source_type": source_type,
                "sync_published_visible": sync_published_visible,
            }
            if params.retired_ids:
                audit_payload["retired_module_ids"] = [str(mid) for mid in params.retired_ids]
            if params.ingestion_instructions is not None:
                audit_payload["ingestion_instructions"] = params.ingestion_instructions
            await record_attribution_event(
                self._db,
                event_type="ingest_started",
                actor=params.uploaded_by,
                source_document_id=doc.id,
                payload=audit_payload,
            )
            return IngestUploadOutcome(
                ingested=IngestedSourceResult(
                    source_document_id=doc.id,
                    title=doc.title,
                    source_type=doc.source_type,
                    stored_path=storage_path,
                )
            )
        finally:
            staging_path.unlink(missing_ok=True)

    async def _stage_and_digest_upload(
        self,
        file: UploadFile,
        *,
        source_type: str,
    ) -> tuple[Path, str, str]:
        """Stream multipart body to a staging file and return path + sha256 + safe name."""
        staging_dir = Path(self._settings.upload_dir) / "ingest_staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f".ingest-{uuid.uuid4()}.part"
        max_media_bytes = self.media_upload_limit_bytes(self._settings.ai_cloud_provider)
        await stream_upload_to_path(
            file,
            staging_path,
            source_type=source_type,
            max_media_bytes=max_media_bytes,
        )
        safe = safe_basename(file.filename or "")
        digest = sha256_hex_file(staging_path)
        return staging_path, digest, safe

    async def _put_staged_upload_to_object_storage(
        self,
        staging_path: Path,
        *,
        original_filename: str,
    ) -> StoredObject:
        """Upload a staged file to MinIO under the ``ingest/`` prefix."""
        object_name = f"ingest/{uuid.uuid4()}_{original_filename}"
        content_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        digest = sha256_hex_file(staging_path)
        return await self._storage.put_object_from_local_file(
            object_name=object_name,
            local_path=staging_path,
            content_type=content_type,
            metadata=build_upload_metadata(content_sha256=digest, original_filename=original_filename),
        )


def _append_bytes_to_path(dest: Path, chunk: bytes, first: bool) -> None:
    mode = "wb" if first else "ab"
    with dest.open(mode) as fh:
        fh.write(chunk)


async def stream_upload_to_path(
    file: UploadFile,
    dest: Path,
    *,
    source_type: str,
    max_media_bytes: int,
) -> None:
    """Stream multipart upload to ``dest``, enforcing the media payload cap."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    bytes_seen = 0
    first = True
    try:
        while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
            bytes_seen += len(chunk)
            if source_type in _MEDIA_SOURCE_TYPES and bytes_seen > max_media_bytes:
                raise IngestValidationError(
                    (
                        f"media upload exceeds {max_media_bytes} bytes; "
                        "larger audio/video requires chunking or provider file upload support"
                    ),
                    status_code=413,
                )
            await anyio.to_thread.run_sync(lambda c=chunk, f=first: _append_bytes_to_path(dest, c, first=f))
            first = False
    except IngestValidationError:
        dest.unlink(missing_ok=True)
        raise
    except Exception:
        dest.unlink(missing_ok=True)
        raise

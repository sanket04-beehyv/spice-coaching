"""Knowledge PDF upload and soft-delete for published-visible source docs.

Owns ``POST /admin/knowledge/upload`` and ``DELETE /admin/knowledge/{id}``.
Does not enqueue the ingest pipeline.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastapi import UploadFile
from mc_contracts.admin_knowledge import KnowledgeSplitSpec
from mc_contracts.enums import ContentDomain
from mc_contracts.errors import ErrorCode
from mc_foundation.objectstore import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStore,
    StoredObject,
    looks_like_object_storage_storage_path,
    safe_basename,
)
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.repositories.file_upload_repository import FileUploadRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.attribution_audit import record_attribution_event
from platform_service.services.file_digest import sha256_hex_file
from platform_service.services.pdf_split import count_pdf_pages, split_pdf_page_range
from platform_service.services.upload_provenance import (
    build_upload_metadata,
    record_file_upload,
)

logger = logging.getLogger(__name__)

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_KNOWLEDGE_OBJECT_PREFIX = "source-documents/knowledge"
_DEFAULT_CONTENT_DOMAIN = ContentDomain.CLINICAL.value
_ALLOWED_THUMBNAIL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class KnowledgeValidationError(AppError):
    """Invalid knowledge upload parameters or file metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = ErrorCode.BAD_REQUEST.value,
    ) -> None:
        super().__init__(code, message, status=status_code)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class KnowledgeUploadedResult:
    source_document_id: uuid.UUID
    title: str
    stored_path: str
    thumbnail_storage_path: str | None
    start_page: int | None
    end_page: int | None


@dataclass(frozen=True)
class _PreparedArtifact:
    local_path: Path
    title: str
    original_filename: str
    thumbnail_storage_path: str | None
    start_page: int | None
    end_page: int | None


class KnowledgeUploadService:
    def __init__(
        self,
        db: AsyncSession,
        storage: ObjectStore,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._storage = storage
        self._settings = settings or get_settings()

    async def retire(self, source_document_id: uuid.UUID) -> None:
        """Soft-delete a knowledge source document by setting ``status='retired'``.

        Only documents with ``sync_published_visible=true`` may be retired.
        Already-retired knowledge documents are a no-op (idempotent).
        """
        source_repo = SourceRepository(self._db)
        doc = await source_repo.get_source_document(source_document_id)
        if doc is None:
            raise KnowledgeValidationError(
                f"source_document {source_document_id} not found",
                status_code=404,
                code=ErrorCode.SOURCE_NOT_FOUND.value,
            )
        if not doc.sync_published_visible:
            raise KnowledgeValidationError(
                "only knowledge documents (sync_published_visible=true) can be retired",
                status_code=403,
                code=ErrorCode.FORBIDDEN.value,
            )
        if doc.status == "retired":
            return
        doc.status = "retired"
        await self._db.flush()

    async def upload(
        self,
        *,
        file: UploadFile,
        uploaded_by: str,
        title: str | None = None,
        thumbnail_storage_path: str | None = None,
        splits_json: str | None = None,
    ) -> list[KnowledgeUploadedResult]:
        splits = self.parse_splits(splits_json)
        staging_path = await self._stage_pdf_upload(file)
        prepared: list[_PreparedArtifact] = []
        split_temps: list[Path] = []
        try:
            page_count = await anyio.to_thread.run_sync(count_pdf_pages, staging_path)
            if page_count < 1:
                raise KnowledgeValidationError("PDF has no pages")

            original_filename = safe_basename(file.filename or "document.pdf")
            if not original_filename.lower().endswith(".pdf"):
                raise KnowledgeValidationError("filename must end with .pdf")

            if splits:
                for index, split in enumerate(splits):
                    self._validate_page_range(split.start_page, split.end_page, page_count, index=index)
                    thumb = await self.validate_thumbnail_storage_path(split.thumbnail_storage_path)
                    split_path = staging_path.parent / f".knowledge-split-{uuid.uuid4()}.pdf"
                    split_temps.append(split_path)
                    try:
                        await anyio.to_thread.run_sync(
                            lambda sp=split, dp=split_path: split_pdf_page_range(
                                staging_path,
                                start_page=sp.start_page,
                                end_page=sp.end_page,
                                dest_path=dp,
                            )
                        )
                    except ValueError as exc:
                        raise KnowledgeValidationError(str(exc)) from exc
                    prepared.append(
                        _PreparedArtifact(
                            local_path=split_path,
                            title=split.title.strip(),
                            original_filename=self._split_filename(
                                original_filename,
                                split.start_page,
                                split.end_page,
                            ),
                            thumbnail_storage_path=thumb,
                            start_page=split.start_page,
                            end_page=split.end_page,
                        )
                    )
            else:
                whole_title = (title or "").strip() or Path(original_filename).stem or original_filename
                thumb = await self.validate_thumbnail_storage_path(thumbnail_storage_path)
                prepared.append(
                    _PreparedArtifact(
                        local_path=staging_path,
                        title=whole_title,
                        original_filename=original_filename,
                        thumbnail_storage_path=thumb,
                        start_page=None,
                        end_page=None,
                    )
                )

            results: list[KnowledgeUploadedResult] = []
            source_repo = SourceRepository(self._db)
            file_upload_repo = FileUploadRepository(self._db)
            for artifact in prepared:
                stored = await self._put_artifact(artifact)
                digest = sha256_hex_file(artifact.local_path)
                await record_file_upload(
                    file_upload_repo=file_upload_repo,
                    bucket_name=stored.bucket_name,
                    object_key=stored.object_name,
                    storage_path=stored.storage_path,
                    original_filename=artifact.original_filename,
                    content_sha256=digest,
                    content_type="application/pdf",
                    size_bytes=stored.size_bytes,
                    uploaded_by=uploaded_by,
                )
                doc = await source_repo.create_source_document(
                    title=artifact.title,
                    source_type="pdf",
                    primary_language=self._settings.deployment_primary_locale,
                    content_domain=_DEFAULT_CONTENT_DOMAIN,
                    original_storage_path=stored.storage_path,
                    content_sha256=digest,
                    original_filename=artifact.original_filename,
                    uploaded_by=uploaded_by,
                    sync_published_visible=True,
                    status="uploaded",
                )
                if artifact.thumbnail_storage_path is not None:
                    doc.thumbnail_storage_path = artifact.thumbnail_storage_path
                    await self._db.flush()
                await record_attribution_event(
                    self._db,
                    event_type="knowledge_uploaded",
                    actor=uploaded_by,
                    source_document_id=doc.id,
                    payload={
                        "stored_path": stored.storage_path,
                        "start_page": artifact.start_page,
                        "end_page": artifact.end_page,
                        "thumbnail_storage_path": artifact.thumbnail_storage_path,
                    },
                )
                results.append(
                    KnowledgeUploadedResult(
                        source_document_id=doc.id,
                        title=doc.title,
                        stored_path=stored.storage_path,
                        thumbnail_storage_path=artifact.thumbnail_storage_path,
                        start_page=artifact.start_page,
                        end_page=artifact.end_page,
                    )
                )
            return results
        finally:
            for path in split_temps:
                path.unlink(missing_ok=True)
            staging_path.unlink(missing_ok=True)

    @staticmethod
    def parse_splits(splits_json: str | None) -> list[KnowledgeSplitSpec]:
        if splits_json is None or not splits_json.strip():
            return []
        try:
            parsed = json.loads(splits_json)
        except json.JSONDecodeError as exc:
            raise KnowledgeValidationError("splits must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise KnowledgeValidationError("splits must be a JSON array")
        if not parsed:
            return []
        splits: list[KnowledgeSplitSpec] = []
        for index, entry in enumerate(parsed):
            if not isinstance(entry, dict):
                raise KnowledgeValidationError(f"splits[{index}] must be an object")
            try:
                split = KnowledgeSplitSpec.model_validate(entry)
            except Exception as exc:
                raise KnowledgeValidationError(f"splits[{index}] is invalid: {exc}") from exc
            if not split.title.strip():
                raise KnowledgeValidationError(f"splits[{index}].title must not be empty")
            if split.start_page > split.end_page:
                raise KnowledgeValidationError(
                    f"splits[{index}]: start_page ({split.start_page}) must be <= end_page ({split.end_page})",
                )
            splits.append(split)
        return splits

    async def validate_thumbnail_storage_path(self, storage_path: str | None) -> str | None:
        if storage_path is None:
            return None
        path = storage_path.strip()
        if not path:
            return None
        bucket_name = self._settings.object_storage_bucket_name
        if not looks_like_object_storage_storage_path(path, bucket_name=bucket_name):
            raise KnowledgeValidationError(
                f"thumbnail_storage_path must be a {bucket_name!r}/ object reference",
            )
        prefix = f"{bucket_name}/"
        object_name = path[len(prefix) :]
        allowed_prefixes = self._settings.admin_file_allowed_prefix_set
        top = object_name.split("/", maxsplit=1)[0]
        if top not in allowed_prefixes or "/" not in object_name:
            raise KnowledgeValidationError(
                f"thumbnail_storage_path object_name must start with one of {sorted(allowed_prefixes)!r}/",
            )
        suffix = Path(object_name).suffix.lower()
        if suffix not in _ALLOWED_THUMBNAIL_SUFFIXES:
            raise KnowledgeValidationError(
                f"thumbnail suffix {suffix!r} is not allowed",
            )
        try:
            await self._storage.stat_object(path)
        except ObjectNotFoundError as exc:
            raise KnowledgeValidationError(
                f"thumbnail object not found for storage_path={path!r}",
            ) from exc
        except (ObjectStorageError, ValueError) as exc:
            raise KnowledgeValidationError(
                "failed to verify thumbnail object in storage",
            ) from exc
        return path

    @staticmethod
    def _validate_page_range(start_page: int, end_page: int, page_count: int, *, index: int) -> None:
        if start_page < 1 or end_page < 1:
            raise KnowledgeValidationError(
                f"splits[{index}]: page numbers must be >= 1",
            )
        if start_page > end_page:
            raise KnowledgeValidationError(
                f"splits[{index}]: start_page ({start_page}) must be <= end_page ({end_page})",
            )
        if end_page > page_count:
            raise KnowledgeValidationError(
                f"splits[{index}]: end_page ({end_page}) exceeds document page count ({page_count})",
            )

    @staticmethod
    def _split_filename(original_filename: str, start_page: int, end_page: int) -> str:
        stem = Path(original_filename).stem or "document"
        return f"{stem}_p{start_page}-{end_page}.pdf"

    async def _stage_pdf_upload(self, file: UploadFile) -> Path:
        if not file.filename:
            raise KnowledgeValidationError(
                "filename is required",
                code=ErrorCode.FILENAME_REQUIRED.value,
            )
        suffix = Path(file.filename).suffix.lower()
        if suffix != ".pdf":
            raise KnowledgeValidationError(
                f"unsupported file type {suffix!r}; only .pdf is accepted",
            )
        staging_dir = Path(self._settings.upload_dir) / "knowledge_staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / f".knowledge-{uuid.uuid4()}.part"
        max_bytes = self._settings.admin_file_max_upload_bytes
        await self._stream_upload_capped(file, staging_path, max_bytes=max_bytes)
        try:
            await anyio.to_thread.run_sync(count_pdf_pages, staging_path)
        except Exception as exc:
            staging_path.unlink(missing_ok=True)
            raise KnowledgeValidationError("uploaded file is not a valid PDF") from exc
        return staging_path

    async def _stream_upload_capped(self, file: UploadFile, dest: Path, *, max_bytes: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        first = True
        try:
            while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise KnowledgeValidationError(
                        "file exceeds maximum allowed size",
                        status_code=413,
                        code=ErrorCode.PAYLOAD_TOO_LARGE.value,
                    )
                await anyio.to_thread.run_sync(
                    lambda c=chunk, f=first: _append_bytes_to_path(dest, c, first=f)
                )
                first = False
        except KnowledgeValidationError:
            dest.unlink(missing_ok=True)
            raise
        except Exception:
            dest.unlink(missing_ok=True)
            raise

    async def _put_artifact(self, artifact: _PreparedArtifact) -> StoredObject:
        object_name = f"{_KNOWLEDGE_OBJECT_PREFIX}/{uuid.uuid4()}_{safe_basename(artifact.original_filename)}"
        digest = sha256_hex_file(artifact.local_path)
        try:
            return await self._storage.put_object_from_local_file(
                object_name=object_name,
                local_path=artifact.local_path,
                content_type="application/pdf",
                metadata=build_upload_metadata(
                    content_sha256=digest,
                    original_filename=artifact.original_filename,
                ),
            )
        except ObjectStorageError:
            logger.exception("Knowledge object storage upload failed for %s", artifact.original_filename)
            raise KnowledgeValidationError(
                "object storage upload failed",
                status_code=502,
                code=ErrorCode.OBJECT_STORAGE_ERROR.value,
            ) from None


def _append_bytes_to_path(dest: Path, chunk: bytes, first: bool) -> None:
    mode = "wb" if first else "ab"
    with dest.open(mode) as fh:
        fh.write(chunk)

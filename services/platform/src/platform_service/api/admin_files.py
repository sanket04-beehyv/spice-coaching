"""Admin file upload and presigned URL endpoints for module editor attachments."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from mc_contracts.errors import ErrorCode
from mc_foundation.objectstore import (
    ContentDisposition,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStore,
    ObjectTooLargeError,
    StoredObject,
    safe_basename,
)
from mc_foundation.problem import AppError
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_user import resolve_spice_actor
from platform_service.config import Settings, get_settings
from platform_service.db.models.file_upload import FileUpload
from platform_service.db.repositories.file_upload_repository import FileUploadRepository
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.file_digest import sha256_hex_file
from platform_service.services.upload_provenance import build_upload_metadata, record_file_upload

logger = logging.getLogger(__name__)

_ALLOWED_UPLOAD_SUFFIXES = {
    ".pdf",
    ".pptx",
    ".docx",
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".webm",
    ".mp4",
    ".mov",
    ".mkv",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
}

_SUFFIX_CONTENT_TYPE: dict[str, str] = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_UPLOAD_CHUNK_BYTES = 1024 * 1024

router = APIRouter(
    prefix="/admin/files",
    tags=["admin-files"],
)


class FileUploadResponse(BaseModel):
    bucket_name: str
    object_name: str
    storage_path: str
    content_type: str
    size_bytes: int = Field(ge=0)
    original_filename: str = Field(description="Client-provided basename at upload time")
    reused_existing: bool = False


class PresignedUrlResponse(BaseModel):
    url: str
    bucket_name: str
    object_name: str
    expires_seconds: int


def _append_bytes_to_path(dest: Path, chunk: bytes, first: bool) -> None:
    mode = "wb" if first else "ab"
    with dest.open(mode) as fh:
        fh.write(chunk)


def _file_upload_response_from_row(row: FileUpload, *, reused_existing: bool = False) -> FileUploadResponse:
    return FileUploadResponse(
        bucket_name=row.bucket_name,
        object_name=row.object_key,
        storage_path=row.storage_path,
        content_type=row.content_type or "application/octet-stream",
        size_bytes=row.size_bytes,
        original_filename=row.original_filename,
        reused_existing=reused_existing,
    )


async def _stream_uploadfile_to_path_capped(
    file: UploadFile,
    dest: Path,
    *,
    max_bytes: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    first = True
    try:
        while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise AppError(
                    ErrorCode.PAYLOAD_TOO_LARGE.value,
                    "file exceeds maximum allowed size",
                    status=413,
                )
            await anyio.to_thread.run_sync(lambda c=chunk, f=first: _append_bytes_to_path(dest, c, first=f))
            first = False
    except Exception:
        dest.unlink(missing_ok=True)
        raise


@router.post("", status_code=201, response_model=FileUploadResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(..., description="File to upload to object storage"),
    storage: ObjectStore = Depends(get_object_storage_client),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> FileUploadResponse:
    """Upload a file into the configured object-storage bucket."""
    if not file.filename:
        raise AppError(ErrorCode.FILENAME_REQUIRED.value, "filename is required", status=400)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise AppError(
            ErrorCode.BAD_REQUEST.value,
            f"unsupported file type {suffix!r}; accepted: {sorted(_ALLOWED_UPLOAD_SUFFIXES)}",
            status=400,
        )

    staging_dir = Path(settings.upload_dir) / "admin_file_staging"
    staging_path = staging_dir / f".upload-{uuid.uuid4()}.part"
    stored: StoredObject | None = None
    safe = safe_basename(file.filename)
    try:
        await _stream_uploadfile_to_path_capped(
            file,
            staging_path,
            max_bytes=settings.admin_file_max_upload_bytes,
        )
        content_sha256 = sha256_hex_file(staging_path)
        existing = await FileUploadRepository(db).find_latest_by_content_sha256(
            bucket_name=storage.bucket_name,
            content_sha256=content_sha256,
        )
        if existing is not None:
            try:
                await storage.stat_object(existing.object_key)
            except ObjectNotFoundError:
                logger.info(
                    "file_upload row for sha256=%s points to missing object %r; re-uploading",
                    content_sha256,
                    existing.object_key,
                )
            else:
                return _file_upload_response_from_row(existing, reused_existing=True)

        object_name = f"{settings.admin_file_upload_prefix}/{uuid.uuid4()}_{safe}"
        content_type = _SUFFIX_CONTENT_TYPE.get(suffix, "application/octet-stream")
        try:
            stored = await storage.put_object_from_local_file(
                object_name=object_name,
                local_path=staging_path,
                content_type=content_type,
                metadata=build_upload_metadata(
                    content_sha256=content_sha256,
                    original_filename=safe,
                ),
            )
        except ValueError as exc:
            raise AppError(ErrorCode.BAD_REQUEST.value, str(exc), status=400) from exc
        except ObjectTooLargeError as exc:
            raise AppError(
                ErrorCode.PAYLOAD_TOO_LARGE.value,
                "file exceeds maximum allowed size",
                status=413,
            ) from exc
        except ObjectStorageError as exc:
            logger.exception("Object storage upload failed for filename=%r", file.filename)
            raise AppError(
                ErrorCode.OBJECT_STORAGE_ERROR.value,
                "object storage upload failed",
                status=502,
            ) from exc
    finally:
        staging_path.unlink(missing_ok=True)

    if stored is None:
        raise AppError(ErrorCode.OBJECT_STORAGE_ERROR.value, "object storage upload failed", status=502)

    await record_file_upload(
        file_upload_repo=FileUploadRepository(db),
        bucket_name=stored.bucket_name,
        object_key=stored.object_name,
        storage_path=stored.storage_path,
        original_filename=safe,
        content_sha256=content_sha256,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        uploaded_by=resolve_spice_actor(request),
    )
    await db.commit()

    return FileUploadResponse(
        bucket_name=stored.bucket_name,
        object_name=stored.object_name,
        storage_path=stored.storage_path,
        content_type=stored.content_type,
        size_bytes=stored.size_bytes,
        original_filename=safe,
        reused_existing=False,
    )


@router.get("/presigned-url", response_model=PresignedUrlResponse)
async def get_presigned_url(
    object_name: str = Query(
        ...,
        description="Object name returned by upload. bucket/object references are also accepted.",
    ),
    expires_seconds: int = Query(
        600,
        ge=1,
        le=86400,
        description="Presigned URL lifetime in seconds, capped at one day.",
    ),
    disposition: ContentDisposition = Query(
        "auto",
        description="'auto' uses inline for PDF/images and attachment otherwise; override with inline|attachment.",
    ),
    storage: ObjectStore = Depends(get_object_storage_client),
    settings: Settings = Depends(get_settings),
) -> PresignedUrlResponse:
    """Return a time-limited GET URL for an uploaded object."""
    if expires_seconds > settings.admin_file_presigned_max_seconds:
        raise AppError(
            ErrorCode.BAD_REQUEST.value,
            f"expires_seconds must be <= {settings.admin_file_presigned_max_seconds}",
            status=400,
        )

    try:
        presigned = await storage.presigned_get_url(
            object_name=object_name,
            expires_seconds=expires_seconds,
            disposition=disposition,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.BAD_REQUEST.value, str(exc), status=400) from exc
    except ObjectNotFoundError as exc:
        raise AppError(ErrorCode.OBJECT_NOT_FOUND.value, "object not found", status=404) from exc
    except ObjectStorageError as exc:
        logger.exception("Presigned URL generation failed for object_name=%r", object_name)
        raise AppError(
            ErrorCode.OBJECT_STORAGE_ERROR.value,
            "object storage presign failed",
            status=502,
        ) from exc

    return PresignedUrlResponse(
        url=presigned.url,
        bucket_name=presigned.bucket_name,
        object_name=presigned.object_name,
        expires_seconds=presigned.expires_seconds,
    )

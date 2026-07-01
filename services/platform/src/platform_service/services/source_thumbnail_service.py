"""Generate and persist source_document thumbnails in MinIO.

Runs before Stage A extraction (separate Celery task). Failures are logged
and swallowed — they must not block the ingest pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.object_storage import (
    ObjectNotFoundError,
    ObjectStorageClient,
    ObjectStorageError,
    looks_like_object_storage_storage_path,
)
from platform_service.services.source_path_materialize import materialize_local_source_file
from platform_service.workers.extractors.media_thumbnail import (
    MediaThumbnailError,
    render_audio_waveform_to_png,
    render_video_frame_to_png,
)
from platform_service.workers.extractors.page_renderer import (
    UnsupportedRenderError,
    render_pdf_page_to_png,
)

logger = logging.getLogger(__name__)

_MVP_SKIP_SOURCE_TYPES = frozenset({"docx", "pptx"})


def source_type_supports_thumbnail(source_type: str) -> bool:
    """Return whether ingest should wait for a thumbnail for this source type."""
    return source_type not in _MVP_SKIP_SOURCE_TYPES


def thumbnail_object_name(source_document_id: UUID) -> str:
    return f"ingest/thumbnails/{source_document_id}.png"


def thumbnail_storage_path(settings: Settings, source_document_id: UUID) -> str:
    return f"{settings.minio_bucket_name}/{thumbnail_object_name(source_document_id)}"


def render_thumbnail_png_bytes(source_path: Path, source_type: str) -> bytes:
    """Return PNG bytes for a local source file. Raises on unsupported or render failure."""
    if source_type in _MVP_SKIP_SOURCE_TYPES:
        raise UnsupportedRenderError(f"thumbnail_skipped_unsupported_source_type source_type={source_type!r}")
    if source_type == "pdf":
        return render_pdf_page_to_png(source_path, page_number=1)
    if source_type == "video":
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "frame.png"
            render_video_frame_to_png(source_path, dest_path=dest)
            return dest.read_bytes()
    if source_type == "audio":
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "waveform.png"
            render_audio_waveform_to_png(source_path, dest_path=dest)
            return dest.read_bytes()
    raise UnsupportedRenderError(f"Unsupported source_type for thumbnail: {source_type!r}")


class SourceThumbnailService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStorageClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._storage = storage or ObjectStorageClient.from_settings(self._settings)
        self._repo = SourceRepository(session)

    async def generate_and_store(
        self,
        *,
        source_document_id: UUID,
        source_path: str,
        source_type: str,
    ) -> str | None:
        """Materialize source, render PNG, upload to MinIO, persist path. Returns path or None."""
        doc = await self._repo.get_source_document(source_document_id)
        if doc is None:
            logger.warning("Thumbnail skipped: source_document %s not found", source_document_id)
            return None

        if source_type in _MVP_SKIP_SOURCE_TYPES:
            logger.info(
                "thumbnail_skipped_unsupported_source_type source_document_id=%s source_type=%s",
                source_document_id,
                source_type,
            )
            return None

        local_path: Path | None = None
        temp_to_delete: Path | None = None
        png_tmp: Path | None = None
        try:
            local_path, temp_to_delete = await materialize_local_source_file(source_path)
            png_bytes = await asyncio.to_thread(
                render_thumbnail_png_bytes,
                local_path,
                source_type,
            )
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                png_tmp = Path(tmp.name)
                tmp.write(png_bytes)

            object_name = thumbnail_object_name(source_document_id)
            await self._storage.put_object_from_local_file(
                object_name=object_name,
                local_path=png_tmp,
                content_type="image/png",
            )
            storage_path = thumbnail_storage_path(self._settings, source_document_id)
            await self._repo.update_thumbnail_storage_path(source_document_id, storage_path)
            await self._session.commit()
            logger.info(
                "Thumbnail stored source_document_id=%s path=%s",
                source_document_id,
                storage_path,
            )
            return storage_path
        except UnsupportedRenderError as exc:
            logger.info(
                "Thumbnail skipped source_document_id=%s: %s",
                source_document_id,
                exc,
            )
            return None
        except (MediaThumbnailError, OSError, ValueError) as exc:
            logger.warning(
                "Thumbnail generation failed source_document_id=%s source_type=%s: %s",
                source_document_id,
                source_type,
                exc,
            )
            return None
        finally:
            if temp_to_delete is not None:
                temp_to_delete.unlink(missing_ok=True)
            if png_tmp is not None:
                png_tmp.unlink(missing_ok=True)


async def presign_thumbnail(
    storage: ObjectStorageClient,
    *,
    thumbnail_storage_path: str | None,
    settings: Settings | None = None,
) -> tuple[str, int] | None:
    """Return (presigned_url, expires_seconds) when a MinIO thumbnail path exists."""
    if not thumbnail_storage_path:
        return None
    settings = settings or get_settings()
    if not looks_like_object_storage_storage_path(
        thumbnail_storage_path, bucket_name=settings.minio_bucket_name
    ):
        return None
    try:
        presigned = await storage.presigned_get_url(
            object_name=thumbnail_storage_path,
            expires_seconds=settings.admin_file_presigned_max_seconds,
            disposition="inline",
        )
    except (ObjectNotFoundError, ObjectStorageError, ValueError) as exc:
        logger.warning("Thumbnail presign failed path=%s: %s", thumbnail_storage_path, exc)
        return None
    return presigned.url, presigned.expires_seconds

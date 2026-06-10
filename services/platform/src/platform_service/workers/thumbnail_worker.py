"""Celery job: generate source_document thumbnail before ingest extraction."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.deps import get_object_storage_client
from platform_service.services.source_thumbnail_service import SourceThumbnailService

logger = logging.getLogger(__name__)


async def run_thumbnail_job(payload: dict[str, Any]) -> None:
    """Generate thumbnail for one source_document. Never raises to the caller."""
    source_document_id = UUID(str(payload["source_document_id"]))
    source_path = str(payload["source_path"])
    source_type = str(payload["source_type"])
    try:
        async with SessionLocal() as session:
            await SourceThumbnailService(session, storage=get_object_storage_client()).generate_and_store(
                source_document_id=source_document_id,
                source_path=source_path,
                source_type=source_type,
            )
    except Exception:
        logger.exception(
            "Thumbnail job crashed source_document_id=%s source_type=%s",
            source_document_id,
            source_type,
        )

"""Module thumbnail defaults and admin path validation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from mc_foundation.objectstore import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStore,
    looks_like_object_storage_storage_path,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.db.validators import ValidationError

_ALLOWED_THUMBNAIL_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})


async def resolve_default_module_thumbnail(
    session: AsyncSession,
    source_document_ids: list[UUID],
) -> str | None:
    """Return the first non-null source_document thumbnail in list order."""
    if not source_document_ids:
        return None
    docs = await SourceRepository(session).list_source_documents_by_ids(source_document_ids)
    by_id = {doc.id: doc for doc in docs}
    for doc_id in source_document_ids:
        doc = by_id.get(doc_id)
        if doc is not None and doc.thumbnail_storage_path:
            return doc.thumbnail_storage_path
    return None


def _object_name_from_storage_path(storage_path: str, *, bucket_name: str) -> str:
    prefix = f"{bucket_name}/"
    if not storage_path.startswith(prefix):
        raise ValidationError(
            "invalid_thumbnail_storage_path",
            f"storage_path must start with {bucket_name!r}/",
        )
    return storage_path[len(prefix) :]


async def validate_module_thumbnail_storage_path(
    storage_path: str | None,
    *,
    settings: Settings | None = None,
    storage: ObjectStore | None = None,
) -> str | None:
    """Validate a module thumbnail MinIO path (``None`` clears the thumbnail)."""
    if storage_path is None:
        return None

    settings = settings or get_settings()
    path = storage_path.strip()
    if not path:
        raise ValidationError("invalid_thumbnail_storage_path", "storage_path must not be empty")

    bucket_name = settings.object_storage_bucket_name
    if not looks_like_object_storage_storage_path(path, bucket_name=bucket_name):
        raise ValidationError(
            "invalid_thumbnail_storage_path",
            f"storage_path must be a {bucket_name!r}/ object reference",
        )

    object_name = _object_name_from_storage_path(path, bucket_name=bucket_name)
    allowed_prefixes = settings.admin_file_allowed_prefix_set
    top = object_name.split("/", maxsplit=1)[0]
    if top not in allowed_prefixes or "/" not in object_name:
        raise ValidationError(
            "invalid_thumbnail_object_prefix",
            f"object_name must start with one of {sorted(allowed_prefixes)!r}/",
        )

    suffix = Path(object_name).suffix.lower()
    if suffix not in _ALLOWED_THUMBNAIL_SUFFIXES:
        raise ValidationError(
            "unsupported_thumbnail_suffix",
            f"thumbnail suffix {suffix!r} is not allowed",
        )

    if storage is not None:
        try:
            await storage.stat_object(path)
        except ObjectNotFoundError as exc:
            raise ValidationError(
                "thumbnail_object_not_found",
                f"object not found for storage_path={path!r}",
            ) from exc
        except (ObjectStorageError, ValueError) as exc:
            raise ValidationError(
                "thumbnail_storage_error",
                "failed to verify thumbnail object in storage",
            ) from exc

    return path

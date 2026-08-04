"""Build the configured ``ObjectStore`` for API / worker processes."""

from __future__ import annotations

from mc_foundation.objectstore import ObjectStore

from platform_service.config import Settings, get_settings
from platform_service.objectstore.s3_store import S3ObjectStore


def create_object_store(*, settings: Settings | None = None) -> ObjectStore:
    """Return the object-storage backend selected by ``OBJECT_STORAGE_BACKEND``.

    Both ``minio`` and ``s3`` use the boto3 adapter with different endpoint /
    credential / auto-create defaults. Unknown values fail fast.
    """
    cfg = settings if settings is not None else get_settings()
    backend = cfg.object_storage_backend
    if backend not in {"minio", "s3"}:
        raise ValueError(f"unsupported OBJECT_STORAGE_BACKEND={backend!r}; supported: 'minio', 's3'")
    return S3ObjectStore(
        bucket_name=cfg.object_storage_bucket_name,
        region=cfg.object_storage_region,
        endpoint=cfg.object_storage_endpoint,
        presigned_endpoint=cfg.object_storage_presigned_endpoint,
        access_key=cfg.object_storage_access_key.get_secret_value(),
        secret_key=cfg.object_storage_secret_key.get_secret_value(),
        secure=cfg.object_storage_secure,
        backend=backend,  # type: ignore[arg-type]
        presign_mode=cfg.object_storage_presign_mode,  # type: ignore[arg-type]
        allowed_prefixes=cfg.admin_file_allowed_prefix_set,
        auto_create_bucket=cfg.object_storage_auto_create_bucket,
    )

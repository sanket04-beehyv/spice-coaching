"""Unit tests for ``create_object_store`` factory."""

from __future__ import annotations

import pytest
from platform_service.config import Settings
from platform_service.objectstore.factory import create_object_store
from platform_service.objectstore.s3_store import S3ObjectStore


def test_create_object_store_minio() -> None:
    settings = Settings(
        object_storage_backend="minio",
        object_storage_endpoint="localhost:9000",
        object_storage_access_key="k",
        object_storage_secret_key="s",
        object_storage_bucket_name="bucket",
        object_storage_secure=False,
    )
    store = create_object_store(settings=settings)
    assert isinstance(store, S3ObjectStore)
    assert store.bucket_name == "bucket"
    store.close()


def test_create_object_store_rejects_unknown_backend() -> None:
    settings = Settings(
        object_storage_backend="minio",
        object_storage_endpoint="localhost:9000",
        object_storage_access_key="k",
        object_storage_secret_key="s",
    )
    # Bypass Settings validator by mutating after construction.
    object.__setattr__(settings, "object_storage_backend", "gcs")
    with pytest.raises(ValueError, match="unsupported OBJECT_STORAGE_BACKEND"):
        create_object_store(settings=settings)

"""Unit tests for ``ObjectStorageClient.check_readiness``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from minio.error import S3Error
from platform_service.services.object_storage import ObjectStorageClient, ObjectStorageError


def _client(*, auto_create_bucket: bool) -> ObjectStorageClient:
    return ObjectStorageClient(
        endpoint="localhost:9000",
        access_key="k",
        secret_key="s",
        bucket_name="medtronics-storage",
        allowed_prefixes=frozenset({"ingest"}),
        auto_create_bucket=auto_create_bucket,
    )


@pytest.mark.asyncio
async def test_check_readiness_ok_when_bucket_exists() -> None:
    client = _client(auto_create_bucket=False)
    client._client = MagicMock()
    client._client.bucket_exists.return_value = True

    await client.check_readiness()

    client._client.bucket_exists.assert_called_once_with("medtronics-storage")


@pytest.mark.asyncio
async def test_check_readiness_raises_on_s3_error() -> None:
    client = _client(auto_create_bucket=False)
    client._client = MagicMock()
    client._client.bucket_exists.side_effect = S3Error(
        "bucket_exists",
        "Access Denied",
        "bucket_exists",
        "host",
        "req",
        None,
        None,
    )

    with pytest.raises(ObjectStorageError, match="readiness check failed"):
        await client.check_readiness()


@pytest.mark.asyncio
async def test_check_readiness_raises_when_bucket_missing_and_no_auto_create() -> None:
    client = _client(auto_create_bucket=False)
    client._client = MagicMock()
    client._client.bucket_exists.return_value = False

    with pytest.raises(ObjectStorageError, match="does not exist"):
        await client.check_readiness()


@pytest.mark.asyncio
async def test_check_readiness_ok_when_bucket_missing_but_auto_create_enabled() -> None:
    client = _client(auto_create_bucket=True)
    client._client = MagicMock()
    client._client.bucket_exists.return_value = False

    await client.check_readiness()

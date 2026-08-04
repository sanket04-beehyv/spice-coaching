"""Unit tests for ``S3ObjectStore.check_readiness`` (mocked boto3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from mc_foundation.objectstore import ObjectStorageError
from platform_service.objectstore.s3_store import S3ObjectStore


def _store(*, auto_create_bucket: bool, backend: str = "minio") -> S3ObjectStore:
    store = S3ObjectStore(
        endpoint="localhost:9000",
        access_key="k",
        secret_key="s",
        bucket_name="medtronics-storage",
        backend=backend,  # type: ignore[arg-type]
        secure=False,
        allowed_prefixes=frozenset({"ingest"}),
        auto_create_bucket=auto_create_bucket,
    )
    store._client = MagicMock()
    store._presign_client = store._client
    return store


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "err"}},
        "HeadBucket",
    )


@pytest.mark.asyncio
async def test_check_readiness_ok_when_bucket_exists() -> None:
    store = _store(auto_create_bucket=False)
    store._client.head_bucket.return_value = {}

    await store.check_readiness()

    store._client.head_bucket.assert_called_once_with(Bucket="medtronics-storage")


@pytest.mark.asyncio
async def test_check_readiness_raises_on_s3_error() -> None:
    store = _store(auto_create_bucket=False)
    store._client.head_bucket.side_effect = _client_error("AccessDenied")

    with pytest.raises(ObjectStorageError, match="readiness check failed"):
        await store.check_readiness()


@pytest.mark.asyncio
async def test_check_readiness_raises_when_bucket_missing_and_no_auto_create() -> None:
    store = _store(auto_create_bucket=False)
    store._client.head_bucket.side_effect = _client_error("404")

    with pytest.raises(ObjectStorageError, match="does not exist"):
        await store.check_readiness()


@pytest.mark.asyncio
async def test_check_readiness_ok_when_bucket_missing_but_auto_create_enabled() -> None:
    store = _store(auto_create_bucket=True)
    store._client.head_bucket.side_effect = _client_error("NoSuchBucket")

    await store.check_readiness()


@pytest.mark.asyncio
async def test_s3_backend_never_auto_creates() -> None:
    store = _store(auto_create_bucket=True, backend="s3")
    assert store._auto_create_bucket is False
    store._client.head_bucket.side_effect = _client_error("404")
    with pytest.raises(ObjectStorageError, match="does not exist"):
        await store.check_readiness()

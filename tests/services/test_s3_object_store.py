"""Unit tests for ``S3ObjectStore`` upload / presign with mocked boto3."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from mc_foundation.objectstore import ObjectNotFoundError, ObjectTooLargeError
from platform_service.objectstore.s3_store import S3ObjectStore


def _store() -> S3ObjectStore:
    store = S3ObjectStore(
        endpoint="localhost:9000",
        access_key="k",
        secret_key="s",
        bucket_name="medtronics-storage",
        backend="minio",
        secure=False,
        allowed_prefixes=frozenset({"uploads", "ingest"}),
        auto_create_bucket=True,
    )
    store._client = MagicMock()
    store._presign_client = store._client
    store._client.head_bucket.return_value = {}
    return store


@pytest.mark.asyncio
async def test_upload_file_puts_object() -> None:
    store = _store()
    stored = await store.upload_file(
        file_obj=BytesIO(b"hello"),
        filename="note.txt",
        prefix="uploads",
    )
    assert stored.bucket_name == "medtronics-storage"
    assert stored.object_name.startswith("uploads/")
    assert stored.size_bytes == 5
    store._client.put_object.assert_called_once()


@pytest.mark.asyncio
async def test_upload_file_rejects_oversized() -> None:
    store = _store()
    with pytest.raises(ObjectTooLargeError):
        await store.upload_file(
            file_obj=BytesIO(b"abcdef"),
            filename="big.bin",
            prefix="uploads",
            max_bytes=3,
        )


@pytest.mark.asyncio
async def test_presigned_get_url() -> None:
    store = _store()
    store._client.head_object.return_value = {}
    store._presign_client.generate_presigned_url.return_value = "https://example/presigned"

    result = await store.presigned_get_url(
        object_name="uploads/a.pdf",
        expires_seconds=60,
        disposition="attachment",
        download_filename="manual.pdf",
    )
    assert result.url == "https://example/presigned"
    assert result.object_name == "uploads/a.pdf"
    store._presign_client.generate_presigned_url.assert_called_once()


@pytest.mark.asyncio
async def test_stat_missing_raises() -> None:
    store = _store()
    store._client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "missing"}},
        "HeadObject",
    )
    with pytest.raises(ObjectNotFoundError):
        await store.stat_object("uploads/missing.txt")


@pytest.mark.asyncio
async def test_put_object_from_local_file(tmp_path: Path) -> None:
    store = _store()
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF")
    stored = await store.put_object_from_local_file(
        object_name="ingest/doc.pdf",
        local_path=src,
        content_type="application/pdf",
        metadata={"sha256": "abc"},
    )
    assert stored.object_name == "ingest/doc.pdf"
    store._client.upload_file.assert_called_once()

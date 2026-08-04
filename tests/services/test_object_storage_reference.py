"""Boundary tests for ``S3ObjectStore.object_name_from_reference``.

Pure unit tests; boto3 client is not contacted for reference normalisation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from platform_service.objectstore.s3_store import S3ObjectStore


@pytest.fixture
def store() -> S3ObjectStore:
    client = S3ObjectStore(
        endpoint="localhost:9000",
        access_key="k",
        secret_key="s",
        bucket_name="medtronics-storage",
        backend="minio",
        secure=False,
        allowed_prefixes=frozenset({"ingest", "uploads", "media", "source-documents"}),
        auto_create_bucket=False,
    )
    client._client = MagicMock()
    client._presign_client = client._client
    return client


def test_object_name_strips_configured_bucket_prefix(store: S3ObjectStore) -> None:
    assert store.object_name_from_reference("medtronics-storage/ingest/a.pdf") == "ingest/a.pdf"


def test_object_name_accepts_key_without_bucket(store: S3ObjectStore) -> None:
    assert store.object_name_from_reference("ingest/a.pdf") == "ingest/a.pdf"


def test_object_name_rejects_foreign_bucket_prefix(store: S3ObjectStore) -> None:
    with pytest.raises(ValueError, match="other-bucket"):
        store.object_name_from_reference("other-bucket/ingest/a.pdf")


def test_object_name_rejects_junk_first_segment(store: S3ObjectStore) -> None:
    with pytest.raises(ValueError, match="nope"):
        store.object_name_from_reference("nope/ingest/a.pdf")


def test_object_name_rejects_empty_reference(store: S3ObjectStore) -> None:
    with pytest.raises(ValueError, match="object_name is required"):
        store.object_name_from_reference("")


def test_object_name_rejects_path_traversal(store: S3ObjectStore) -> None:
    with pytest.raises(ValueError, match="unsafe path segment"):
        store.object_name_from_reference("ingest/../etc/passwd")


def test_object_name_normalises_leading_slash(store: S3ObjectStore) -> None:
    assert store.object_name_from_reference("/ingest/a.pdf") == "ingest/a.pdf"

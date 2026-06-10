"""Boundary tests for ``ObjectStorageClient.object_name_from_reference``.

This helper normalises caller-supplied storage references (either bare
object keys or ``{bucket}/{key}`` forms) into a key within the
configured bucket. It must refuse references that name a different
bucket — that guards against an accidental cross-bucket read after a
misconfigured rename or a confused caller — and enforces the configured
``allowed_prefixes`` set so the upload path cannot reach arbitrary keys.

Pure unit tests, no external services.
"""

from __future__ import annotations

import pytest
from platform_service.services.object_storage import ObjectStorageClient


@pytest.fixture
def client() -> ObjectStorageClient:
    return ObjectStorageClient(
        endpoint="localhost:9000",
        access_key="k",
        secret_key="s",
        bucket_name="medtronics-storage",
        allowed_prefixes=frozenset({"ingest", "uploads", "media", "source-documents"}),
        auto_create_bucket=False,
    )


def test_object_name_strips_configured_bucket_prefix(client: ObjectStorageClient) -> None:
    assert client.object_name_from_reference("medtronics-storage/ingest/a.pdf") == "ingest/a.pdf"


def test_object_name_accepts_key_without_bucket(client: ObjectStorageClient) -> None:
    assert client.object_name_from_reference("ingest/a.pdf") == "ingest/a.pdf"


def test_object_name_rejects_foreign_bucket_prefix(client: ObjectStorageClient) -> None:
    with pytest.raises(ValueError, match="other-bucket"):
        client.object_name_from_reference("other-bucket/ingest/a.pdf")


def test_object_name_rejects_junk_first_segment(client: ObjectStorageClient) -> None:
    """A first segment that's neither this bucket nor an allowed prefix
    must be refused — silent fall-through would let callers reach
    arbitrary keys."""
    with pytest.raises(ValueError, match="nope"):
        client.object_name_from_reference("nope/ingest/a.pdf")


def test_object_name_rejects_empty_reference(client: ObjectStorageClient) -> None:
    with pytest.raises(ValueError, match="object_name is required"):
        client.object_name_from_reference("")


def test_object_name_rejects_path_traversal(client: ObjectStorageClient) -> None:
    """``..`` segments must be refused — a caller that partially controls the
    object reference must not be able to walk out of the prefix."""
    with pytest.raises(ValueError, match="unsafe path segment"):
        client.object_name_from_reference("ingest/../etc/passwd")


def test_object_name_normalises_leading_slash(client: ObjectStorageClient) -> None:
    """A leading slash is stripped — handlers that received the key
    from a URL with a leading ``/`` still resolve to the right object."""
    assert client.object_name_from_reference("/ingest/a.pdf") == "ingest/a.pdf"

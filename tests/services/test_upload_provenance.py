"""Tests for upload provenance helpers."""

from platform_service.services.upload_provenance import (
    META_FILENAME,
    META_SHA256,
    build_upload_metadata,
    parse_upload_metadata,
)


def test_build_upload_metadata_returns_lowercase_simple_keys() -> None:
    assert build_upload_metadata(content_sha256="abc", original_filename="manual.pdf") == {
        META_SHA256: "abc",
        META_FILENAME: "manual.pdf",
    }


def test_parse_upload_metadata_strips_x_amz_meta_prefix_and_lowercases() -> None:
    sha, name = parse_upload_metadata(
        {"X-Amz-Meta-Content-Sha256": "abc", "x-amz-meta-original-filename": "manual.pdf"}
    )
    assert sha == "abc"
    assert name == "manual.pdf"


def test_parse_upload_metadata_handles_already_normalised_keys() -> None:
    sha, name = parse_upload_metadata({"content-sha256": "xyz", "original-filename": "x.docx"})
    assert sha == "xyz"
    assert name == "x.docx"


def test_parse_upload_metadata_none_returns_pair_of_none() -> None:
    assert parse_upload_metadata(None) == (None, None)


def test_parse_upload_metadata_missing_keys_returns_none() -> None:
    assert parse_upload_metadata({"unrelated": "value"}) == (None, None)

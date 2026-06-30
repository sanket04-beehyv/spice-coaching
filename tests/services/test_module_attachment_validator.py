"""Unit tests for module_json attachment validation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.config import Settings
from platform_service.db.validators import ValidationError
from platform_service.services.module_attachment_validator import (
    parse_youtube_url,
    validate_module_attachments,
)
from platform_service.services.object_storage import ObjectNotFoundError


def _file_ref(*, attachment_id: str | None = None, object_name: str = "media/test.pdf") -> dict:
    aid = attachment_id or str(uuid4())
    return {
        "kind": "file",
        "attachment_id": aid,
        "storage_path": f"medtronics-storage/{object_name}",
        "object_name": object_name,
        "content_type": "application/pdf",
        "media_kind": "pdf",
        "original_filename": "test.pdf",
    }


class _FakeStorage:
    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists

    async def stat_object(self, object_name: str) -> None:
        if not self._exists:
            raise ObjectNotFoundError(f"missing {object_name}")


class TestParseYoutubeUrl:
    def test_watch_url(self) -> None:
        url, vid = parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert vid == "dQw4w9WgXcQ"
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_short_link(self) -> None:
        url, vid = parse_youtube_url("https://youtu.be/dQw4w9WgXcQ")
        assert vid == "dQw4w9WgXcQ"
        assert "watch?v=dQw4w9WgXcQ" in url


@pytest.mark.asyncio
class TestValidateModuleAttachments:
    async def test_accepts_module_and_card_file_refs(self) -> None:
        aid = str(uuid4())
        module_json = {
            "attachments": [_file_ref(attachment_id=aid)],
            "cards": [
                {
                    "title": {"bn": "C"},
                    "body": {"bn": "B"},
                    "card_family_id": str(uuid4()),
                    "attachments": [_file_ref()],
                }
            ],
        }
        out = await validate_module_attachments(
            module_json,
            settings=Settings(),
            storage=_FakeStorage(),
        )
        assert out is not None
        assert len(out["attachments"]) == 1
        assert len(out["cards"][0]["attachments"]) == 1

    async def test_normalizes_youtube_url(self) -> None:
        aid = str(uuid4())
        module_json = {
            "attachments": [
                {
                    "kind": "youtube",
                    "attachment_id": aid,
                    "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
                }
            ],
            "cards": [],
        }
        out = await validate_module_attachments(module_json, settings=Settings())
        assert out is not None
        att = out["attachments"][0]
        assert att["youtube_video_id"] == "dQw4w9WgXcQ"
        assert att["youtube_url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    async def test_rejects_wrong_object_prefix(self) -> None:
        module_json = {"attachments": [_file_ref(object_name="uploads/wrong.pdf")], "cards": []}
        with pytest.raises(ValidationError) as exc_info:
            await validate_module_attachments(module_json, settings=Settings())
        assert exc_info.value.code == "invalid_attachment_object_prefix"

    async def test_rejects_duplicate_attachment_id(self) -> None:
        aid = str(uuid4())
        ref = _file_ref(attachment_id=aid)
        module_json = {"attachments": [ref, ref], "cards": []}
        with pytest.raises(ValidationError) as exc_info:
            await validate_module_attachments(module_json, settings=Settings())
        assert exc_info.value.code == "duplicate_attachment_id"

    async def test_rejects_missing_minio_object(self) -> None:
        module_json = {"attachments": [_file_ref()], "cards": []}
        with pytest.raises(ValidationError) as exc_info:
            await validate_module_attachments(
                module_json,
                settings=Settings(),
                storage=_FakeStorage(exists=False),
            )
        assert exc_info.value.code == "attachment_object_not_found"

    async def test_rejects_media_kind_mismatch(self) -> None:
        ref = _file_ref(object_name="media/clip.mp4")
        ref["media_kind"] = "pdf"
        module_json = {"attachments": [ref], "cards": []}
        with pytest.raises(ValidationError) as exc_info:
            await validate_module_attachments(module_json, settings=Settings())
        assert exc_info.value.code == "attachment_media_kind_mismatch"

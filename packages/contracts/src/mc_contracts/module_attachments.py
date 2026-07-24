"""Module attachment refs stored inline in ``module.module_json``.

Files are uploaded via ``POST /admin/files`` (MinIO); this DTO holds the
object path reference. YouTube links store a normalized URL only (no MinIO).
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ModuleAttachmentKind(str, enum.Enum):
    FILE = "file"
    YOUTUBE = "youtube"


class ModuleMediaKind(str, enum.Enum):
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    PDF = "pdf"


class ModuleAttachmentFileRef(BaseModel):
    """Reference to a file already stored in object storage."""

    kind: Literal["file"] = "file"
    attachment_id: str = Field(..., description="UUID string; stable within a module version")
    label: str | None = None
    sort_order: int = 0
    storage_path: str = Field(..., description="Full bucket/path from upload response")
    object_name: str = Field(..., description="Object key within the bucket, e.g. media/{uuid}_file.pdf")
    content_type: str
    original_filename: str | None = None
    media_kind: ModuleMediaKind


class ModuleAttachmentYoutubeRef(BaseModel):
    """External YouTube link (no MinIO object)."""

    kind: Literal["youtube"] = "youtube"
    attachment_id: str = Field(..., description="UUID string; stable within a module version")
    label: str | None = None
    sort_order: int = 0
    youtube_url: str = Field(..., description="Canonical watch URL")
    youtube_video_id: str | None = Field(
        None,
        description="Denormalized video id for embed UIs",
    )


ModuleAttachmentRef = Annotated[
    ModuleAttachmentFileRef | ModuleAttachmentYoutubeRef,
    Field(discriminator="kind"),
]

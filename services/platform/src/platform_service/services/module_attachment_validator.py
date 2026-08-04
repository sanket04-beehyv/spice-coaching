"""Validate and normalize ``module_json`` attachment refs before module edit."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from mc_contracts.module_attachments import (
    ModuleAttachmentFileRef,
    ModuleAttachmentYoutubeRef,
    ModuleMediaKind,
)
from mc_foundation.objectstore import ObjectNotFoundError, ObjectStore
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from platform_service.config import Settings
from platform_service.db.validators import ValidationError

_ATTACHMENT_ADAPTER: TypeAdapter[ModuleAttachmentFileRef | ModuleAttachmentYoutubeRef] = TypeAdapter(
    ModuleAttachmentFileRef | ModuleAttachmentYoutubeRef
)

_ALLOWED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".mp3",
        ".wav",
        ".m4a",
        ".flac",
        ".ogg",
        ".webm",
        ".mp4",
        ".mov",
        ".mkv",
    }
)

_SUFFIX_TO_MEDIA_KIND: dict[str, ModuleMediaKind] = {
    ".pdf": ModuleMediaKind.PDF,
    ".jpg": ModuleMediaKind.IMAGE,
    ".jpeg": ModuleMediaKind.IMAGE,
    ".png": ModuleMediaKind.IMAGE,
    ".webp": ModuleMediaKind.IMAGE,
    ".mp3": ModuleMediaKind.AUDIO,
    ".wav": ModuleMediaKind.AUDIO,
    ".m4a": ModuleMediaKind.AUDIO,
    ".flac": ModuleMediaKind.AUDIO,
    ".ogg": ModuleMediaKind.AUDIO,
    ".webm": ModuleMediaKind.AUDIO,
    ".mp4": ModuleMediaKind.VIDEO,
    ".mov": ModuleMediaKind.VIDEO,
    ".mkv": ModuleMediaKind.VIDEO,
}

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"})
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def media_kind_for_suffix(suffix: str) -> ModuleMediaKind:
    try:
        return _SUFFIX_TO_MEDIA_KIND[suffix]
    except KeyError as exc:
        raise ValidationError(
            "unsupported_attachment_suffix",
            f"unsupported attachment suffix {suffix!r}",
        ) from exc


def parse_youtube_url(url: str) -> tuple[str, str]:
    """Return ``(canonical_watch_url, video_id)`` or raise ``ValidationError``."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("invalid_youtube_url", "youtube_url must use http or https")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise ValidationError("invalid_youtube_url", f"unsupported youtube host {host!r}")

    video_id: str | None = None
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/", maxsplit=1)[0] or None
    else:
        qs = parse_qs(parsed.query)
        ids = qs.get("v") or []
        video_id = ids[0] if ids else None
        if not video_id and parsed.path.startswith("/shorts/"):
            video_id = parsed.path.split("/shorts/", maxsplit=1)[-1].split("/", maxsplit=1)[0] or None

    if not video_id or not _YOUTUBE_ID_RE.match(video_id):
        raise ValidationError("invalid_youtube_url", "could not extract a valid youtube video id")

    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return canonical, video_id


def _parse_attachment(raw: Any, *, location: str) -> ModuleAttachmentFileRef | ModuleAttachmentYoutubeRef:
    if not isinstance(raw, dict):
        raise ValidationError("invalid_attachment", f"{location}: attachment must be an object")
    try:
        att = _ATTACHMENT_ADAPTER.validate_python(raw)
    except PydanticValidationError as exc:
        raise ValidationError("invalid_attachment", f"{location}: {exc}") from exc
    try:
        UUID(str(att.attachment_id))
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid_attachment_id", f"{location}: attachment_id must be a UUID") from exc
    return att


def _validate_file_ref(
    att: ModuleAttachmentFileRef,
    *,
    location: str,
    allowed_prefixes: frozenset[str],
) -> ModuleAttachmentFileRef:
    object_name = att.object_name.strip().lstrip("/")
    top = object_name.split("/", maxsplit=1)[0]
    if top not in allowed_prefixes or "/" not in object_name:
        raise ValidationError(
            "invalid_attachment_object_prefix",
            f"{location}: object_name must start with one of {sorted(allowed_prefixes)!r}/",
        )

    suffix = Path(object_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValidationError(
            "unsupported_attachment_suffix",
            f"{location}: suffix {suffix!r} is not allowed for module attachments",
        )

    expected_kind = media_kind_for_suffix(suffix)
    if att.media_kind != expected_kind:
        raise ValidationError(
            "attachment_media_kind_mismatch",
            f"{location}: media_kind {att.media_kind.value!r} does not match suffix {suffix!r}",
        )

    if not att.storage_path.strip():
        raise ValidationError("invalid_attachment_storage_path", f"{location}: storage_path is required")

    return att.model_copy(update={"object_name": object_name})


def _normalize_youtube_ref(att: ModuleAttachmentYoutubeRef, *, location: str) -> ModuleAttachmentYoutubeRef:
    canonical, video_id = parse_youtube_url(att.youtube_url)
    return att.model_copy(update={"youtube_url": canonical, "youtube_video_id": video_id})


def _attachment_to_dict(att: ModuleAttachmentFileRef | ModuleAttachmentYoutubeRef) -> dict[str, Any]:
    return att.model_dump(mode="json")


async def validate_module_attachments(
    module_json: dict[str, Any] | None,
    *,
    settings: Settings,
    storage: ObjectStore | None = None,
) -> dict[str, Any] | None:
    """Validate attachment refs and return a sanitized ``module_json`` copy.

    Raises ``ValidationError`` on invariant violations. When ``storage`` is
    provided, each file ref is checked with ``stat_object``.
    """
    if module_json is None:
        return None

    out = copy.deepcopy(module_json)
    allowed_prefixes = settings.admin_file_allowed_prefix_set
    seen_ids: set[str] = set()

    module_attachments = out.get("attachments")
    if module_attachments is None:
        out["attachments"] = []
        module_attachments = out["attachments"]
    if not isinstance(module_attachments, list):
        raise ValidationError("invalid_module_attachments", "module_json.attachments must be a list")

    if len(module_attachments) > settings.module_attachment_max_per_module:
        raise ValidationError(
            "too_many_module_attachments",
            f"module-level attachments exceed max {settings.module_attachment_max_per_module}",
        )

    normalized_module: list[dict[str, Any]] = []
    for idx, raw in enumerate(module_attachments):
        location = f"attachments[{idx}]"
        att = _parse_attachment(raw, location=location)
        if att.attachment_id in seen_ids:
            raise ValidationError("duplicate_attachment_id", f"{location}: duplicate attachment_id")
        seen_ids.add(att.attachment_id)
        if isinstance(att, ModuleAttachmentFileRef):
            att = _validate_file_ref(att, location=location, allowed_prefixes=allowed_prefixes)
            if storage is not None:
                try:
                    await storage.stat_object(att.object_name)
                except ObjectNotFoundError as exc:
                    raise ValidationError(
                        "attachment_object_not_found",
                        f"{location}: object {att.object_name!r} not found in storage",
                    ) from exc
        else:
            att = _normalize_youtube_ref(att, location=location)
        normalized_module.append(_attachment_to_dict(att))
    out["attachments"] = normalized_module

    cards = out.get("cards")
    if cards is None:
        return out
    if not isinstance(cards, list):
        raise ValidationError("invalid_module_cards", "module_json.cards must be a list")

    normalized_cards: list[Any] = []
    for card_idx, card in enumerate(cards):
        if not isinstance(card, dict):
            normalized_cards.append(card)
            continue
        card_copy = copy.deepcopy(card)
        card_attachments = card_copy.get("attachments")
        if card_attachments is None:
            card_copy["attachments"] = []
            normalized_cards.append(card_copy)
            continue
        if not isinstance(card_attachments, list):
            raise ValidationError(
                "invalid_card_attachments",
                f"cards[{card_idx}].attachments must be a list",
            )
        if len(card_attachments) > settings.module_attachment_max_per_card:
            raise ValidationError(
                "too_many_card_attachments",
                f"cards[{card_idx}] exceeds max {settings.module_attachment_max_per_card} attachments",
            )
        normalized_card_atts: list[dict[str, Any]] = []
        for att_idx, raw in enumerate(card_attachments):
            location = f"cards[{card_idx}].attachments[{att_idx}]"
            att = _parse_attachment(raw, location=location)
            if att.attachment_id in seen_ids:
                raise ValidationError("duplicate_attachment_id", f"{location}: duplicate attachment_id")
            seen_ids.add(att.attachment_id)
            if isinstance(att, ModuleAttachmentFileRef):
                att = _validate_file_ref(att, location=location, allowed_prefixes=allowed_prefixes)
                if storage is not None:
                    try:
                        await storage.stat_object(att.object_name)
                    except ObjectNotFoundError as exc:
                        raise ValidationError(
                            "attachment_object_not_found",
                            f"{location}: object {att.object_name!r} not found in storage",
                        ) from exc
            else:
                att = _normalize_youtube_ref(att, location=location)
            normalized_card_atts.append(_attachment_to_dict(att))
        card_copy["attachments"] = normalized_card_atts
        normalized_cards.append(card_copy)

    out["cards"] = normalized_cards
    return out

"""Shared helpers for module repository read/write mixins."""

from __future__ import annotations

import re
from uuid import UUID

# Sentinel: caller did not supply thumbnail_storage_path (copy forward on version bump).
THUMBNAIL_UNSET: object = object()


def slugify(text: str) -> str:
    """Crude slug for module_code derivation. Bangla survives via raw chars."""
    cleaned = re.sub(r"\s+", "-", (text or "").strip().lower())
    cleaned = re.sub(r"[^\w\-]+", "", cleaned, flags=re.UNICODE)
    return cleaned[:80] or "module"


def gap_code_for_module(module_id: UUID) -> str:
    return f"module_primary_gap_{str(module_id).replace('-', '_')}"


class ModuleNotFoundError(Exception):
    """Raised when a requested module does not exist (or has been retired)."""

    def __init__(self, module_id: UUID) -> None:
        super().__init__(f"module {module_id} not found")
        self.module_id = module_id

"""Convert parsed block content to plain text for downstream LLM stages.

This module is kept for backwards-compat with older imports/tests. The
implementation lives in the service layer so Stage D can sanitize legacy
content_block text without importing from `workers/`.
"""

from __future__ import annotations

from platform_service.services.plain_text import block_content_to_plain_text

__all__ = ["block_content_to_plain_text"]

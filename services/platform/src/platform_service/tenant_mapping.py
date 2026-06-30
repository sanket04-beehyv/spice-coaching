"""Parse SPICE tenantId → platform UUID configuration."""

from __future__ import annotations

import json
from uuid import UUID


def parse_spice_tenant_id_map(raw: str) -> dict[int, UUID]:
    """Parse ``SPICE_TENANT_ID_MAP`` from JSON or ``id=uuid,id=uuid`` form."""
    text = (raw or "").strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in SPICE_TENANT_ID_MAP: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("SPICE_TENANT_ID_MAP JSON must be an object")
        out: dict[int, UUID] = {}
        for key, value in loaded.items():
            spice_id = int(key)
            out[spice_id] = UUID(str(value))
        return out

    out: dict[int, UUID] = {}
    for part in text.split(","):
        chunk = part.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError("SPICE_TENANT_ID_MAP entries must be JSON or comma-separated id=uuid pairs")
        spice_raw, uuid_raw = chunk.split("=", 1)
        out[int(spice_raw.strip())] = UUID(uuid_raw.strip())
    return out

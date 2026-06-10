"""Tests for SPICE tenantId → platform UUID mapping."""

from __future__ import annotations

from uuid import UUID

import pytest
from platform_service.tenant_mapping import parse_spice_tenant_id_map

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")


def test_parse_json_map() -> None:
    raw = '{"1": "11111111-1111-1111-1111-111111111111", "2": "22222222-2222-2222-2222-222222222222"}'
    parsed = parse_spice_tenant_id_map(raw)
    assert parsed[1] == TENANT_A
    assert parsed[2] == TENANT_B


def test_parse_comma_separated_map() -> None:
    raw = "1=11111111-1111-1111-1111-111111111111,2=22222222-2222-2222-2222-222222222222"
    parsed = parse_spice_tenant_id_map(raw)
    assert parsed[1] == TENANT_A
    assert parsed[2] == TENANT_B


def test_parse_empty_returns_empty_dict() -> None:
    assert parse_spice_tenant_id_map("") == {}


def test_parse_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_spice_tenant_id_map("{")

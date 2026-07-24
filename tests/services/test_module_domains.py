"""Tests for module.domain catalog helpers."""

from __future__ import annotations

from platform_service.module_domains import (
    catalog_domain_label,
    module_domain_catalog_for_prompt,
    normalize_module_domain_label,
)


def test_normalize_module_domain_label() -> None:
    assert normalize_module_domain_label("  Hypertension ") == "hypertension"
    assert normalize_module_domain_label("family-planning") == "family_planning"


def test_catalog_domain_label_normalizes_any_snake_case_label() -> None:
    assert catalog_domain_label("ANC") == "anc"
    assert catalog_domain_label("rmnch") == "rmnch"
    assert catalog_domain_label("Dengue Fever") == "dengue_fever"
    assert catalog_domain_label("unknown_topic") == "unknown_topic"


def test_catalog_domain_label_rejects_empty() -> None:
    assert catalog_domain_label(None) is None
    assert catalog_domain_label("   ") is None


def test_module_domain_catalog_for_prompt_is_sorted() -> None:
    labels = module_domain_catalog_for_prompt().split(", ")
    assert labels == sorted(labels)

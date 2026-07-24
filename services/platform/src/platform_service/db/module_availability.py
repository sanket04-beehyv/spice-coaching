"""Shared predicates for module admin availability."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_
from sqlalchemy.sql import true

from platform_service.db.models.module import Module

LIFECYCLE_PUBLISHED = "published"
LIFECYCLE_DEACTIVATED = "deactivated"
LIFECYCLE_RETIRED = "retired"
LIFECYCLE_DRAFT = "draft"
VALID_LIFECYCLE_STATUSES = frozenset(
    {LIFECYCLE_DRAFT, LIFECYCLE_PUBLISHED, LIFECYCLE_RETIRED, LIFECYCLE_DEACTIVATED}
)


def is_training_module_family() -> ColumnElement[bool]:
    """True when a module family participates in CHW training workflows."""
    return Module.chatbot_faqs_only.is_(False)


def analytics_timestamp_in_range(column, from_dt, to_dt):
    """SQL expression for optional date-range filtering in analytics CASE branches."""
    clauses = []
    if from_dt is not None:
        clauses.append(column >= from_dt)
    if to_dt is not None:
        clauses.append(column <= to_dt)
    if not clauses:
        return true()
    return and_(*clauses)

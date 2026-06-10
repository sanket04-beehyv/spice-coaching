"""Shared tenant scoping helpers for repository queries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute


def tenant_scope_filter(
    column: InstrumentedAttribute[UUID | None],
    tenant_id: UUID,
) -> ColumnElement[bool]:
    """Match tenant-global rows (``tenant_id IS NULL``) and tenant-specific rows."""
    return or_(column.is_(None), column == tenant_id)

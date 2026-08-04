"""Read queries for admin ingestion-run list views."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.models.source_document import SourceDocument

INGESTION_RUN_SORT_KEYS = frozenset({"started_at", "completed_at", "status", "document_label"})
INGESTION_RUN_SORT_DIRS = frozenset({"asc", "desc"})
DEFAULT_INGESTION_RUN_SORT_BY = "started_at"
DEFAULT_INGESTION_RUN_SORT_DIR = "desc"


def _escape_ilike_pattern(value: str) -> str:
    """Escape SQL ``LIKE``/``ILIKE`` wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _document_label_expr() -> Any:
    """Trimmed filename when non-empty, else title — matches IngestionRunPresenter."""
    trimmed_filename = func.trim(SourceDocument.original_filename)
    return case(
        (func.coalesce(trimmed_filename, "") != "", trimmed_filename),
        else_=SourceDocument.title,
    )


def _order_clauses(
    sort_by: str,
    sort_dir: str,
) -> list[Any]:
    descending = sort_dir == "desc"
    order_fn = (lambda col: col.desc()) if descending else (lambda col: col.asc())

    if sort_by == "started_at":
        primary = order_fn(IngestionRun.started_at)
    elif sort_by == "completed_at":
        completed = IngestionRun.completed_at
        if descending:
            primary = completed.desc().nullslast()
        else:
            primary = completed.asc().nullsfirst()
    elif sort_by == "status":
        primary = order_fn(IngestionRun.status)
    elif sort_by == "document_label":
        primary = order_fn(_document_label_expr())
    else:
        raise ValueError(f"unsupported sort_by: {sort_by}")

    return [primary, order_fn(IngestionRun.id)]


class IngestionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _ingestion_runs_filtered_stmt(
        self,
        *,
        status: str | None = None,
        filename_query: str | None = None,
        sort_by: str = DEFAULT_INGESTION_RUN_SORT_BY,
    ) -> Select[tuple[IngestionRun]]:
        stmt = select(IngestionRun)
        if status is not None:
            stmt = stmt.where(IngestionRun.status == status)
        if sort_by == "document_label" or filename_query:
            stmt = stmt.join(
                SourceDocument,
                IngestionRun.source_document_id == SourceDocument.id,
            )
        if filename_query:
            escaped = _escape_ilike_pattern(filename_query.strip())
            pattern = f"%{escaped}%"
            stmt = stmt.where(
                or_(
                    SourceDocument.original_filename.ilike(pattern, escape="\\"),
                    SourceDocument.title.ilike(pattern, escape="\\"),
                )
            )
        return stmt

    async def count_ingestion_runs(
        self,
        *,
        status: str | None = None,
        filename_query: str | None = None,
    ) -> int:
        base = self._ingestion_runs_filtered_stmt(status=status, filename_query=filename_query)
        count_stmt = select(func.count()).select_from(
            base.with_only_columns(IngestionRun.id, maintain_column_froms=True).subquery()
        )
        result = await self._session.execute(count_stmt)
        return int(result.scalar_one())

    async def list_ingestion_runs(
        self,
        *,
        status: str | None = None,
        filename_query: str | None = None,
        sort_by: str = DEFAULT_INGESTION_RUN_SORT_BY,
        sort_dir: str = DEFAULT_INGESTION_RUN_SORT_DIR,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IngestionRun]:
        stmt = (
            self._ingestion_runs_filtered_stmt(
                status=status,
                filename_query=filename_query,
                sort_by=sort_by,
            )
            .order_by(*_order_clauses(sort_by, sort_dir))
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

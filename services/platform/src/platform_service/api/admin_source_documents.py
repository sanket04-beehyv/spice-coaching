"""Admin source document catalog — list ingested documents for dashboard dropdowns."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from mc_contracts.admin_modules import SourceDocumentListResponse, SourceDocumentSummary
from mc_contracts.enums import SourceDocumentType
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.deps import get_db

router = APIRouter(prefix="/admin", tags=["admin-source-documents"])

VALID_SOURCE_DOCUMENT_STATUSES = frozenset({"ingesting", "ingested", "failed"})
VALID_SOURCE_DOCUMENT_TYPES = frozenset(e.value for e in SourceDocumentType)


def _summary_from_document(doc: SourceDocument) -> SourceDocumentSummary:
    return SourceDocumentSummary(
        id=doc.id,
        title=doc.title,
        source_type=doc.source_type,
        status=doc.status,
        content_domain=doc.content_domain,
        original_filename=doc.original_filename,
        ingested_at=doc.ingested_at,
    )


def _normalize_source_types(raw: list[str] | None) -> list[str] | None:
    """Accept repeated params and/or comma-separated values; dedupe, preserve order."""
    if not raw:
        return None
    types: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in item.split(","):
            value = part.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            types.append(value)
    return types or None


@router.get("/source-documents", response_model=SourceDocumentListResponse)
async def list_source_documents(
    status: str | None = Query(
        "ingested",
        description="ingesting | ingested | failed (default: ingested)",
    ),
    source_type: list[str] | None = Query(
        None,
        description=(
            "Optional filter; repeat and/or comma-separate: "
            "pdf | pptx | docx | audio | video "
            "(e.g. source_type=video&source_type=audio or source_type=video,audio)"
        ),
    ),
    q: str | None = Query(
        None,
        description="Case-insensitive substring match on original_filename or title",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> SourceDocumentListResponse:
    if status is not None and status not in VALID_SOURCE_DOCUMENT_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of: {', '.join(sorted(VALID_SOURCE_DOCUMENT_STATUSES))}",
        )
    source_types = _normalize_source_types(source_type)
    if source_types is not None:
        invalid = [t for t in source_types if t not in VALID_SOURCE_DOCUMENT_TYPES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"source_type must be one of: {', '.join(sorted(VALID_SOURCE_DOCUMENT_TYPES))}; "
                    f"got invalid: {', '.join(invalid)}"
                ),
            )
    filename_query = q.strip() if q and q.strip() else None
    repo = SourceRepository(session)
    list_filters = {
        "status": status,
        "source_types": source_types,
        "filename_query": filename_query,
    }
    total_source_documents = await repo.count_source_documents(**list_filters)
    docs = await repo.list_source_documents(**list_filters, limit=limit, offset=offset)
    total_pages = (total_source_documents + limit - 1) // limit if total_source_documents > 0 else 0
    return SourceDocumentListResponse(
        source_documents=[_summary_from_document(doc) for doc in docs],
        total_source_documents=total_source_documents,
        total_pages=total_pages,
        limit=limit,
        offset=offset,
    )

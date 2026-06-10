"""Pipeline outcome types shared by orchestrator and stage runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID


@dataclass
class StageOutcome:
    """Per-stage execution summary (one entry in PipelineResult.stages)."""

    stage: str
    status: str  # succeeded | failed | skipped
    summary: dict | None = None
    error: dict | None = None


@dataclass
class PipelineResult:
    """End-to-end outcome of one orchestrator run."""

    run_id: UUID
    source_document_id: UUID
    final_status: str  # succeeded | failed | partially_succeeded
    stages: list[StageOutcome] = field(default_factory=list)
    candidates_emitted: int = 0
    drafts_produced: int = 0

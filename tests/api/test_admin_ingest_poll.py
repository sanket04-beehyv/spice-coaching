"""Unit tests for admin ingest poll serialisation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from platform_service.services.ingestion_run_presenter import IngestionRunPresenter
from platform_service.services.run_state_service import (
    FUSION_RUN_TYPE,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_CROSS_SOURCE_FUSION,
    STEP_RUNNING,
    STEP_SUCCEEDED,
)


def _step(
    *,
    stage: str = STAGE_CARD_DRAFT,
    status: str = STEP_RUNNING,
    input_summary: dict | None = None,
    output_summary: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        status=status,
        started_at=datetime.now(UTC),
        completed_at=None,
        input_summary_jsonb=input_summary,
        output_summary_jsonb=output_summary,
        error_jsonb=None,
    )


class TestRunKind:
    def test_pipeline_by_default(self) -> None:
        run = SimpleNamespace(error_jsonb=None)
        assert IngestionRunPresenter.run_kind(run) == "pipeline"

    def test_fusion_run(self) -> None:
        run = SimpleNamespace(error_jsonb={"type": FUSION_RUN_TYPE})
        assert IngestionRunPresenter.run_kind(run) == FUSION_RUN_TYPE


class TestStepToPollDict:
    def test_running_merge_activity(self) -> None:
        cand = str(uuid4())
        step = _step(
            input_summary={
                "activity": "published_module_merge",
                "candidate_id": cand,
            },
        )
        out = IngestionRunPresenter.step_to_poll_dict(step)
        assert out["activity"] == "published_module_merge"
        assert "published_module_merge" not in out

    def test_terminal_card_draft_merge_outcome(self) -> None:
        merged_from = str(uuid4())
        step = _step(
            status=STEP_SUCCEEDED,
            input_summary={"candidate_id": str(uuid4())},
            output_summary={
                "was_published_merge": True,
                "merged_from_module_id": merged_from,
            },
        )
        out = IngestionRunPresenter.step_to_poll_dict(step)
        assert out["published_module_merge"] == {
            "active": False,
            "was_merge": True,
            "merged_from_module_id": merged_from,
        }

    def test_fusion_card_draft_flag(self) -> None:
        step = _step(input_summary={"candidate_id": str(uuid4()), "fusion": True})
        out = IngestionRunPresenter.step_to_poll_dict(step)
        assert out["fusion"] is True


class TestCurrentActivityFromSteps:
    def test_published_module_merge(self) -> None:
        cand = str(uuid4())
        steps = [
            _step(
                input_summary={
                    "activity": "published_module_merge",
                    "candidate_id": cand,
                },
            )
        ]
        activity = IngestionRunPresenter.current_activity_from_steps(steps, run_status=RUN_RUNNING)
        assert activity == {
            "kind": "published_module_merge",
            "stage": STAGE_CARD_DRAFT,
            "candidate_id": cand,
        }

    def test_cross_source_fusion_activity(self) -> None:
        steps = [
            _step(
                stage=STAGE_CROSS_SOURCE_FUSION,
                input_summary={"activity": "cross_source_fusion"},
            )
        ]
        activity = IngestionRunPresenter.current_activity_from_steps(steps, run_status=RUN_RUNNING)
        assert activity == {
            "kind": "cross_source_fusion",
            "stage": STAGE_CROSS_SOURCE_FUSION,
        }

    def test_none_when_run_not_running(self) -> None:
        steps = [_step(input_summary={"activity": "published_module_merge"})]
        assert IngestionRunPresenter.current_activity_from_steps(steps, run_status=RUN_SUCCEEDED) is None

    def test_fusion_draft_without_activity_not_current(self) -> None:
        """Fusion card_draft steps expose ``fusion`` on the step dict, not current_activity."""
        steps = [
            _step(
                input_summary={
                    "candidate_id": str(uuid4()),
                    "fusion": True,
                    "merged_title": "Fused ANC",
                },
            )
        ]
        assert IngestionRunPresenter.current_activity_from_steps(steps, run_status=RUN_RUNNING) is None
        assert IngestionRunPresenter.step_to_poll_dict(steps[0])["fusion"] is True

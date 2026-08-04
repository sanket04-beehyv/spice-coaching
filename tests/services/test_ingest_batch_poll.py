"""Unit tests for ingest batch progress catalog, tree assembly, and status rollup."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from platform_service.services.ingest_batch_poll_presenter import (
    _retry_targets_for_run,
    build_run_tree,
)
from platform_service.services.ingest_progress_catalog import catalog_entry
from platform_service.services.run_state_service import (
    BATCH_FAILED,
    BATCH_PARTIALLY_SUCCEEDED,
    BATCH_QUEUED,
    BATCH_RUNNING,
    BATCH_SUCCEEDED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_CROSS_SOURCE_FUSION,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STAGE_QUIZ_GENERATION,
    STAGE_THUMBNAIL,
    STEP_AWAITING_INPUT,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SUCCEEDED,
    rollup_batch_status,
)


def _step(
    *,
    stage: str,
    status: str = STEP_SUCCEEDED,
    candidate_id: str | None = None,
    chunk_id: str | None = None,
    activity: str | None = None,
    error: dict | None = None,
    output_summary: dict | None = None,
) -> SimpleNamespace:
    input_summary: dict = {}
    if candidate_id is not None:
        input_summary["candidate_id"] = candidate_id
    if chunk_id is not None:
        input_summary["chunk_id"] = chunk_id
    if activity is not None:
        input_summary["activity"] = activity
    return SimpleNamespace(
        id=uuid4(),
        stage=stage,
        status=status,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC) if status == STEP_SUCCEEDED else None,
        input_summary_jsonb=input_summary or None,
        output_summary_jsonb=output_summary,
        error_jsonb=error,
        error_code=None,
        error_message=None,
    )


def _candidate(
    cand_id: object,
    *,
    proposed_title: str = "X",
    source_chunk_ids: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=cand_id,
        proposed_title=proposed_title,
        source_chunk_ids=source_chunk_ids,
    )


class TestCatalog:
    def test_known_stage(self) -> None:
        title, description = catalog_entry(STAGE_EXTRACT)
        assert title == "Extracting content"
        assert "outline" in description.lower()

    def test_activity_variant(self) -> None:
        title, _ = catalog_entry(STAGE_CARD_DRAFT, activity="published_module_merge")
        assert "Merging" in title


class TestRollupBatchStatus:
    def test_empty_is_queued(self) -> None:
        assert rollup_batch_status([]) == BATCH_QUEUED

    def test_any_running(self) -> None:
        assert rollup_batch_status([RUN_SUCCEEDED, RUN_RUNNING]) == BATCH_RUNNING

    def test_queued_before_running_check_order(self) -> None:
        assert rollup_batch_status([RUN_QUEUED, RUN_SUCCEEDED]) == BATCH_QUEUED

    def test_all_succeeded(self) -> None:
        assert rollup_batch_status([RUN_SUCCEEDED, RUN_SUCCEEDED]) == BATCH_SUCCEEDED

    def test_all_failed(self) -> None:
        assert rollup_batch_status([RUN_FAILED, RUN_FAILED]) == BATCH_FAILED

    def test_mixed_partial(self) -> None:
        assert rollup_batch_status([RUN_SUCCEEDED, RUN_FAILED]) == BATCH_PARTIALLY_SUCCEEDED


class TestBuildRunTree:
    def test_shared_stages_and_candidate_nested_under_chunk(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_THUMBNAIL),
            _step(stage=STAGE_EXTRACT),
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand)),
            _step(stage=STAGE_QUIZ_GENERATION, candidate_id=str(cand), status=STEP_RUNNING),
        ]
        candidate = _candidate(cand, proposed_title="ANC Counselling", source_chunk_ids=["chunk-1"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        assert [n["key"] for n in tree] == [
            STAGE_THUMBNAIL,
            STAGE_EXTRACT,
            STAGE_MODULE_IDENTIFY,
        ]
        identify = tree[-1]
        assert identify["status"] == STEP_RUNNING
        assert [c["key"] for c in identify["children"]] == ["chunk"]
        chunk = identify["children"][0]
        assert chunk["chunk_id"] == "chunk-1"
        assert chunk["status"] == STEP_RUNNING
        assert [c["key"] for c in chunk["children"]] == ["candidate"]
        branch = chunk["children"][0]
        assert branch["candidate_id"] == str(cand)
        assert branch["proposed_title"] == "ANC Counselling"
        assert "ANC Counselling" in branch["description"]
        assert [c["key"] for c in branch["children"]] == [STAGE_CARD_DRAFT, STAGE_QUIZ_GENERATION]
        assert all("title" in n and "description" in n for n in tree)
        assert all("title" in c and "description" in c for c in branch["children"])

    def test_invents_module_identify_parent_when_missing(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand)),
            _step(stage=STAGE_QUIZ_GENERATION, candidate_id=str(cand), status=STEP_RUNNING),
        ]
        candidate = _candidate(cand, proposed_title="Invented Parent Case", source_chunk_ids=["chunk-1"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        assert [n["key"] for n in tree] == [STAGE_MODULE_IDENTIFY]
        identify = tree[0]
        assert identify["status"] == STEP_RUNNING
        assert identify["title"]
        assert identify["description"]
        assert [c["key"] for c in identify["children"]] == ["chunk"]
        chunk = identify["children"][0]
        assert chunk["children"][0]["key"] == "candidate"
        assert [c["key"] for c in chunk["children"][0]["children"]] == [
            STAGE_CARD_DRAFT,
            STAGE_QUIZ_GENERATION,
        ]

    def test_empty_when_no_steps(self) -> None:
        assert build_run_tree(steps=[], candidates=[]) == []

    def test_candidates_nested_under_chunks_not_siblings(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_EXTRACT),
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1", status=STEP_SUCCEEDED),
            _step(
                stage=STAGE_MODULE_IDENTIFY,
                chunk_id="chunk-2",
                status=STEP_FAILED,
                error={"type": "Timeout", "message": "ai-runtime"},
            ),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand)),
        ]
        candidate = _candidate(cand, proposed_title="From chunk-1", source_chunk_ids=["chunk-1"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        identify = tree[-1]
        assert identify["key"] == STAGE_MODULE_IDENTIFY
        assert identify["status"] == "partially_succeeded"
        keys = [c["key"] for c in identify["children"]]
        assert keys == ["chunk", "chunk"]
        assert identify["children"][0]["chunk_id"] == "chunk-1"
        assert identify["children"][1]["chunk_id"] == "chunk-2"
        assert identify["children"][1]["status"] == STEP_FAILED
        assert identify["children"][1]["error"]["type"] == "Timeout"
        assert [c["key"] for c in identify["children"][0]["children"]] == ["candidate"]
        assert identify["children"][0]["children"][0]["candidate_id"] == str(cand)
        assert identify["children"][1]["children"] == []

    def test_chunk_status_rolls_up_nested_candidate(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand), status=STEP_RUNNING),
        ]
        candidate = _candidate(cand, source_chunk_ids=["chunk-1"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        identify = tree[-1]
        assert identify["status"] == STEP_RUNNING
        assert [c["key"] for c in identify["children"]] == ["chunk"]
        chunk = identify["children"][0]
        assert chunk["status"] == STEP_RUNNING
        assert chunk["children"][0]["key"] == "candidate"

    def test_multi_chunk_lineage_nests_under_first_id_only(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-2"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand)),
        ]
        candidate = _candidate(
            cand,
            proposed_title="Merged",
            source_chunk_ids=["chunk-2", "chunk-1"],
        )
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        identify = tree[-1]
        chunk1, chunk2 = identify["children"]
        assert chunk1["chunk_id"] == "chunk-1"
        assert chunk2["chunk_id"] == "chunk-2"
        assert chunk1["children"] == []
        assert [c["candidate_id"] for c in chunk2["children"]] == [str(cand)]

    def test_orphan_candidate_omitted(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand), status=STEP_RUNNING),
        ]
        candidate = _candidate(cand, source_chunk_ids=None)
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        identify = tree[-1]
        assert identify["children"][0]["children"] == []
        # Identify reflects chunk identify only (succeeded), not the orphan draft.
        assert identify["status"] == STEP_SUCCEEDED

    def test_unknown_chunk_id_omitted(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand)),
        ]
        candidate = _candidate(cand, source_chunk_ids=["chunk-missing"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        assert tree[-1]["children"][0]["children"] == []

    def test_awaiting_input_rolls_up_through_chunk(self) -> None:
        cand = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand), status=STEP_AWAITING_INPUT),
        ]
        candidate = _candidate(cand, source_chunk_ids=["chunk-1"])
        tree = build_run_tree(steps=steps, candidates=[candidate])  # type: ignore[arg-type]
        identify = tree[-1]
        chunk = identify["children"][0]
        assert chunk["children"][0]["status"] == STEP_AWAITING_INPUT
        assert chunk["status"] == STEP_AWAITING_INPUT
        assert identify["status"] == STEP_AWAITING_INPUT

    def test_near_dups_nest_under_own_chunks(self) -> None:
        cand_a = uuid4()
        cand_b = uuid4()
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1"),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-2"),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand_a)),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand_b)),
        ]
        candidates = [
            _candidate(cand_a, proposed_title="ANC A", source_chunk_ids=["chunk-1"]),
            _candidate(cand_b, proposed_title="ANC B", source_chunk_ids=["chunk-2"]),
        ]
        tree = build_run_tree(steps=steps, candidates=candidates)  # type: ignore[arg-type]
        chunk1, chunk2 = tree[-1]["children"]
        assert [c["candidate_id"] for c in chunk1["children"]] == [str(cand_a)]
        assert [c["candidate_id"] for c in chunk2["children"]] == [str(cand_b)]


class TestRetryTargetsForRun:
    def test_failed_extract(self) -> None:
        batch_id = uuid4()
        run_id = uuid4()
        run = SimpleNamespace(id=run_id, status=RUN_FAILED)
        steps = [
            _step(stage=STAGE_THUMBNAIL),
            _step(stage=STAGE_EXTRACT, status=STEP_FAILED, error={"message": "boom"}),
        ]
        targets = _retry_targets_for_run(
            batch_id=batch_id,
            run=run,  # type: ignore[arg-type]
            steps=steps,  # type: ignore[arg-type]
            blocked_by_active_claim=False,
        )
        assert targets == [
            {
                "run_id": str(run_id),
                "stage": STAGE_EXTRACT,
            }
        ]

    def test_failed_identify_chunk_includes_chunk_id(self) -> None:
        batch_id = uuid4()
        run_id = uuid4()
        run = SimpleNamespace(id=run_id, status=RUN_FAILED)
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY),
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-2", status=STEP_FAILED),
        ]
        targets = _retry_targets_for_run(
            batch_id=batch_id,
            run=run,  # type: ignore[arg-type]
            steps=steps,  # type: ignore[arg-type]
            blocked_by_active_claim=False,
        )
        assert len(targets) == 1
        assert targets[0]["stage"] == STAGE_MODULE_IDENTIFY
        assert targets[0]["chunk_id"] == "chunk-2"
        assert "candidate_id" not in targets[0]

    def test_failed_card_draft_includes_candidate_id(self) -> None:
        batch_id = uuid4()
        run_id = uuid4()
        cand = uuid4()
        run = SimpleNamespace(id=run_id, status=RUN_FAILED)
        steps = [_step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand), status=STEP_FAILED)]
        targets = _retry_targets_for_run(
            batch_id=batch_id,
            run=run,  # type: ignore[arg-type]
            steps=steps,  # type: ignore[arg-type]
            blocked_by_active_claim=False,
        )
        assert targets == [
            {
                "run_id": str(run_id),
                "stage": STAGE_CARD_DRAFT,
                "candidate_id": str(cand),
            }
        ]

    def test_active_claim_blocks_all_targets(self) -> None:
        batch_id = uuid4()
        run = SimpleNamespace(id=uuid4(), status=RUN_RUNNING)
        steps = [_step(stage=STAGE_EXTRACT, status=STEP_FAILED)]
        assert (
            _retry_targets_for_run(
                batch_id=batch_id,
                run=run,  # type: ignore[arg-type]
                steps=steps,  # type: ignore[arg-type]
                blocked_by_active_claim=True,
            )
            == []
        )

    def test_no_failures_returns_empty(self) -> None:
        batch_id = uuid4()
        run = SimpleNamespace(id=uuid4(), status=RUN_SUCCEEDED)
        steps = [_step(stage=STAGE_EXTRACT), _step(stage=STAGE_MODULE_IDENTIFY)]
        assert (
            _retry_targets_for_run(
                batch_id=batch_id,
                run=run,  # type: ignore[arg-type]
                steps=steps,  # type: ignore[arg-type]
                blocked_by_active_claim=False,
            )
            == []
        )

    def test_fusion_failure(self) -> None:
        batch_id = uuid4()
        run_id = uuid4()
        run = SimpleNamespace(id=run_id, status=RUN_FAILED)
        steps = [_step(stage=STAGE_CROSS_SOURCE_FUSION, status=STEP_FAILED)]
        targets = _retry_targets_for_run(
            batch_id=batch_id,
            run=run,  # type: ignore[arg-type]
            steps=steps,  # type: ignore[arg-type]
            blocked_by_active_claim=False,
        )
        assert targets == [
            {
                "run_id": str(run_id),
                "stage": STAGE_CROSS_SOURCE_FUSION,
            }
        ]

    def test_multiple_failures(self) -> None:
        batch_id = uuid4()
        run_id = uuid4()
        cand = uuid4()
        run = SimpleNamespace(id=run_id, status=RUN_FAILED)
        steps = [
            _step(stage=STAGE_MODULE_IDENTIFY, chunk_id="chunk-1", status=STEP_FAILED),
            _step(stage=STAGE_CARD_DRAFT, candidate_id=str(cand), status=STEP_FAILED),
            _step(stage=STAGE_QUIZ_GENERATION, candidate_id=str(cand), status=STEP_SUCCEEDED),
        ]
        targets = _retry_targets_for_run(
            batch_id=batch_id,
            run=run,  # type: ignore[arg-type]
            steps=steps,  # type: ignore[arg-type]
            blocked_by_active_claim=False,
        )
        assert len(targets) == 2
        assert targets[0]["chunk_id"] == "chunk-1"
        assert targets[1]["candidate_id"] == str(cand)
        assert targets[1]["stage"] == STAGE_CARD_DRAFT

"""Tests for semantic clustering of chat FAQ candidates."""

from __future__ import annotations

from datetime import UTC, datetime

from platform_service.services.chat_faq_aggregator import CandidateQuestion
from platform_service.services.chat_faq_clusterer import cluster_questions


def _candidate(text: str, count: int) -> CandidateQuestion:
    return CandidateQuestion(
        text=text,
        normalized_text=text,
        occurrence_count=count,
        last_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


class TestClusterQuestions:
    def test_merges_similar_embeddings_to_target_count(self) -> None:
        questions = [
            _candidate("child cough 14 days", 5),
            _candidate("cough for two weeks in child", 4),
            _candidate("blood pressure cuff", 3),
            _candidate("how to measure bp", 2),
            _candidate("fever in infant", 2),
            _candidate("baby has fever", 2),
            _candidate("respiratory rate child", 1),
            _candidate("count breaths per minute", 1),
        ]
        # Similar pairs share nearly identical unit vectors.
        embeddings = [
            [1.0, 0.0, 0.0],
            [0.99, 0.01, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.99, 0.01],
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 0.99],
            [0.7, 0.0, 0.7],
            [0.71, 0.0, 0.69],
        ]

        clusters = cluster_questions(questions, embeddings, target_count=3)
        assert len(clusters) == 3
        assert clusters[0].total_count >= clusters[1].total_count

    def test_returns_one_cluster_per_question_when_below_target(self) -> None:
        questions = [_candidate("child cough", 3), _candidate("fever", 2)]
        embeddings = [[1.0, 0.0], [0.0, 1.0]]
        clusters = cluster_questions(questions, embeddings, target_count=6)
        assert len(clusters) == 2

"""Semantic clustering of chat question candidates via embeddings."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from platform_service.config import Settings, get_settings
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.chat_faq_aggregator import CandidateQuestion
from platform_service.services.embedding_vector import assert_embedding_dimension


@dataclass
class QuestionCluster:
    members: list[CandidateQuestion] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    total_count: int = 0
    last_seen_at: datetime | None = None
    seed_text: str = ""

    def add_member(self, question: CandidateQuestion, embedding: list[float]) -> None:
        self.members.append(question)
        self.embeddings.append(embedding)
        self.total_count += question.occurrence_count
        if question.last_seen_at is not None and (
            self.last_seen_at is None or question.last_seen_at > self.last_seen_at
        ):
            self.last_seen_at = question.last_seen_at
        if not self.seed_text or question.occurrence_count >= max(
            member.occurrence_count for member in self.members
        ):
            self.seed_text = question.text


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _l2_norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec))


def _normalize(vec: list[float]) -> list[float]:
    norm = _l2_norm(vec)
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    return _dot(a, b)


def _weighted_centroid(clusters: list[list[float]], weights: list[int]) -> list[float]:
    if not clusters:
        return []
    dim = len(clusters[0])
    total_weight = sum(weights)
    if total_weight <= 0:
        return _normalize(clusters[0])
    centroid = [0.0] * dim
    for vec, weight in zip(clusters, weights, strict=True):
        for i, value in enumerate(vec):
            centroid[i] += value * weight
    return _normalize(centroid)


def _cluster_centroid(cluster: QuestionCluster) -> list[float]:
    weights = [member.occurrence_count for member in cluster.members]
    normalized_embeddings = [_normalize(vec) for vec in cluster.embeddings]
    return _weighted_centroid(normalized_embeddings, weights)


def cluster_questions(
    questions: list[CandidateQuestion],
    embeddings: list[list[float]],
    *,
    target_count: int,
) -> list[QuestionCluster]:
    """Agglomeratively merge question clusters until at most ``target_count`` remain."""
    if not questions:
        return []
    if len(questions) != len(embeddings):
        raise ValueError("questions and embeddings length mismatch")

    clusters: list[QuestionCluster] = []
    for question, embedding in zip(questions, embeddings, strict=True):
        cluster = QuestionCluster()
        cluster.add_member(question, embedding)
        clusters.append(cluster)

    while len(clusters) > target_count:
        best_i = -1
        best_j = -1
        best_sim = -2.0
        centroids = [_cluster_centroid(cluster) for cluster in clusters]
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                sim = _cosine_similarity(centroids[i], centroids[j])
                if sim > best_sim:
                    best_sim = sim
                    best_i = i
                    best_j = j
        if best_i < 0 or best_j < 0:
            break

        merged = QuestionCluster()
        for member, embedding in zip(
            clusters[best_i].members + clusters[best_j].members,
            clusters[best_i].embeddings + clusters[best_j].embeddings,
            strict=True,
        ):
            merged.add_member(member, embedding)

        remaining = [cluster for idx, cluster in enumerate(clusters) if idx not in {best_i, best_j}]
        remaining.append(merged)
        clusters = remaining

    clusters.sort(key=lambda c: c.total_count, reverse=True)
    return clusters


class ChatFaqClusterer:
    def __init__(
        self,
        ai: AIRuntimeClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._ai = ai
        self._settings = settings or get_settings()

    async def cluster(
        self,
        questions: list[CandidateQuestion],
        *,
        target_count: int | None = None,
    ) -> list[QuestionCluster]:
        if not questions:
            return []

        target = target_count if target_count is not None else self._settings.chat_faq_target_count
        texts = [question.text for question in questions]
        raw_embeddings = await self._ai.embed(texts)
        expected_dim = self._settings.embedding_dimension
        embeddings = [assert_embedding_dimension(vec, expected_dim=expected_dim) for vec in raw_embeddings]
        return cluster_questions(questions, embeddings, target_count=target)

"""Exhaustiveness guards for assessment-due trigger catalog."""

from __future__ import annotations

from platform_service.services.assessment_topic_catalog import (
    FOLLOW_UP_CRITERIA_BUCKETS,
    REFERRAL_REASON_DISPLAY_NAMES,
    canonical_assessment_topic_keys,
    topics_for_display_reason,
    topics_for_follow_up_criteria_bucket,
)


def test_referral_reason_display_names_map_to_canonical_topics() -> None:
    canonical = canonical_assessment_topic_keys()
    unmapped: list[str] = []
    for display_name in REFERRAL_REASON_DISPLAY_NAMES:
        topics = topics_for_display_reason(display_name)
        if not topics or not any(topic in canonical for topic in topics):
            unmapped.append(display_name)
    assert not unmapped, f"unmapped referral display names: {unmapped}"


def test_follow_up_criteria_buckets_map_to_canonical_topics() -> None:
    canonical = canonical_assessment_topic_keys()
    unmapped: list[str] = []
    for bucket in FOLLOW_UP_CRITERIA_BUCKETS:
        topics = topics_for_follow_up_criteria_bucket(bucket)
        if not topics or not any(topic in canonical for topic in topics):
            unmapped.append(bucket)
    assert not unmapped, f"unmapped follow-up criteria buckets: {unmapped}"

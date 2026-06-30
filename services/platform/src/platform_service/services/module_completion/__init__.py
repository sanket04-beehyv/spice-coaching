"""Module completion event handlers — quiz progress, gap escalation, learning points."""

from platform_service.services.module_completion.gap_escalation_handler import GapEscalationHandler
from platform_service.services.module_completion.learning_points_handler import LearningPointsHandler
from platform_service.services.module_completion.quiz_escalation_handler import QuizEscalationHandler
from platform_service.services.module_completion.quiz_progress_handler import QuizProgressHandler
from platform_service.services.module_completion.telemetry_parsing import (
    coerce_tenant_uuid,
    module_quiz_outcome_kind,
    parse_chw_id,
    parse_quiz_id,
    parse_quiz_score_pct,
    parse_uuid,
    spice_outcome_is_incorrect,
)

__all__ = [
    "GapEscalationHandler",
    "LearningPointsHandler",
    "QuizEscalationHandler",
    "QuizProgressHandler",
    "coerce_tenant_uuid",
    "module_quiz_outcome_kind",
    "parse_chw_id",
    "parse_quiz_id",
    "parse_quiz_score_pct",
    "parse_uuid",
    "spice_outcome_is_incorrect",
]

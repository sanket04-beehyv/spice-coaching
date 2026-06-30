"""Stable domain enums — shared across platform and ai-runtime."""

import enum


class ContentDomain(str, enum.Enum):
    """Source document content domain (v3.3 ingest / Stage C branching)."""

    DIGITAL = "digital"
    CLINICAL = "clinical"
    CLINICAL_WITH_APP_ACTION = "clinical_with_app_action"
    SUPERVISOR_UPDATE = "supervisor_update"


class AssessmentMode(str, enum.Enum):
    """Whether post-publish quiz generation runs for modules from this source."""

    WITH_QUIZ = "with_quiz"
    READ_ONLY = "read_only"


class ClinicalDomain(str, enum.Enum):
    HYPERTENSION = "hypertension"
    DIABETES = "diabetes"
    MATERNAL_HEALTH = "maternal_health"
    EMERGENCY = "emergency"
    SPICE_DIGITAL = "spice_digital"


class ActionType(str, enum.Enum):
    PROTOCOL_CLARIFICATION = "protocol_clarification"
    ESCALATION = "escalation"
    COUNSELLING = "counselling"
    EQUIPMENT_GUIDANCE = "equipment_guidance"
    PRE_VISIT_PREP = "pre_visit_prep"


class RiskLevel(str, enum.Enum):
    ROUTINE = "routine"
    MODERATE = "moderate"
    HIGH = "high"
    EMERGENCY = "emergency"


class DocumentStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class LearningPathStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class LearningPathSource(str, enum.Enum):
    DEFAULT_PROGRAM = "default_program"
    GAP_DRIVEN = "gap_driven"
    SUPERVISOR_ASSIGNED = "supervisor_assigned"
    DIGITAL_REINFORCEMENT = "digital_reinforcement"


class GenerationType(str, enum.Enum):
    """Internal AI runtime generation task types.

    Legacy types (counselling, it_help, extraction, quiz) drive the v1
    coaching surface. v3.3 types drive the new content pipeline:
    - OUTLINE_INFERENCE  — Stage B LLM fallback when markdown headings are weak
    - MODULE_IDENTIFICATION — Stage C corpus-level module candidate identification
    - CARD_DRAFTING      — Stage D bilingual card drafting
    - QUIZ_DRAFTING      — Stage D scenario-based quiz generation
    - DISTRACTOR_CRITIQUE — Stage D second-pass distractor scoring
    - BILINGUAL_TRANSLATION — Stage D gap-fill when one language is silent
    - VISION_EXTRACTION  — Stage A vision fallback (page image → markdown)
    """

    # v3.3 content pipeline types
    OUTLINE_INFERENCE = "outline_inference"
    MODULE_IDENTIFICATION = "module_identification"
    # Stage 2b: cross-source fusion. Operates on per-source candidate metadata
    # (titles + scopes + source_document_id) — NOT raw corpus content. Identifies
    # candidate pairs/triples that cover the same CHW behavioural unit from
    # different angles (e.g., clinical reasoning from a training manual + the
    # corresponding app workflow from a digital workflow guide) and merges them
    # into a fused candidate citing source_provenance from each constituent.
    CROSS_SOURCE_FUSION = "cross_source_fusion"
    CARD_DRAFTING = "card_drafting"
    # Stage 2-draft: match a new candidate to an existing published module and
    # merge card sets (new content wins on conflict).
    MODULE_PUBLISHED_MERGE = "module_published_merge"
    QUIZ_DRAFTING = "quiz_drafting"
    DISTRACTOR_CRITIQUE = "distractor_critique"
    BILINGUAL_TRANSLATION = "bilingual_translation"
    VISION_EXTRACTION = "vision_extraction"
    # Grounded coaching Q&A over the published v3.3 module corpus
    # (platform /coaching/rag-query). Replaces the prior IT_HELP misuse.
    COACHING_RAG = "coaching_rag"
    # Post-publish: map a drafted module to seeded behavioural_gap registry codes.
    MODULE_GAP_CLASSIFICATION = "module_gap_classification"
    # Post-publish: map a drafted module to assessment-due topic triggers.
    MODULE_ASSESSMENT_TOPIC_CLASSIFICATION = "module_assessment_topic_classification"
    # Post-publish: generate bilingual search metadata for lexical retrieval.
    MODULE_SEARCH_METADATA = "module_search_metadata"
    # Post-publish: generate per-card bilingual search metadata for BM25 retrieval.
    CARD_SEARCH_METADATA = "card_search_metadata"
    # Nightly: synthesize bilingual chat FAQ chips from clustered telemetry.
    CHAT_FAQ_SYNTHESIS = "chat_faq_synthesis"


class SourceDocumentType(str, enum.Enum):
    """Values written by admin ingest to ``source_document.source_type``.

    Surfaced on the RAG attribution response so the UI can render a
    type-appropriate preview (page-in-PDF vs. timecode-in-video).
    """

    PDF = "pdf"
    PPTX = "pptx"
    DOCX = "docx"
    AUDIO = "audio"
    VIDEO = "video"


# ── Event family / type constants (ClickHouse telemetry) ─────────────────────


class EventFamily(str, enum.Enum):
    """Event family for telemetry events."""

    LEARNING = "learning"
    COACHING = "coaching"
    CLINICAL_OBSERVED = "clinical_observed"
    DIGITAL = "digital"
    SYSTEM = "system"


class CoachingEventType(str, enum.Enum):
    """Event type are the sub types of event family."""

    CARD_SHOWN = "card_shown"
    CARD_ACCEPTED = "card_accepted"
    CARD_SKIPPED = "card_skipped"
    AUDIO_PLAYED = "audio_played"
    QUIZ_STARTED = "quiz_started"
    QUIZ_ANSWERED = "quiz_answered"
    COUNSELLING_USED = "counselling_used"
    SPICE_ACTION_OBSERVED = "spice_action_observed"
    RISK_FLAG_OBSERVED = "risk_flag_observed"
    EQUIPMENT_ANOMALY_OBSERVED = "equipment_anomaly_observed"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SYNC_STARTED = "sync_started"
    SYNC_COMPLETED = "sync_completed"
    # ── v3.3 module-pipeline events (W-10) ─────────────────────────────
    # Carry module_family_id (UUID) on the TelemetryEvent. Distinct from the
    # scenario-level CARD_* / QUIZ_* events above which carry scenario_id.
    # MODULE_DELIVERED:   the module surfaced in the CHW's morning rotation.
    # MODULE_CARD_VIEWED: CHW opened a specific card within a module.
    # MODULE_QUIZ_ATTEMPTED: CHW finished the module quiz; payload carries
    #                       quiz_score_pct (0.0–1.0) and per-question answers.
    MODULE_DELIVERED = "module_delivered"
    MODULE_CARD_VIEWED = "module_card_viewed"
    MODULE_QUIZ_ATTEMPTED = "module_quiz_attempted"


class DigitalEventType(str, enum.Enum):
    """Digital event types are sub types of digital event family."""

    SYNC_ATTEMPT = "sync_attempt"
    LOGIN_ATTEMPT = "login_attempt"
    FORM_SUBMIT = "form_submit"
    DIGITAL_HELP_USED = "digital_help_used"


# ── Telemetry (ClickHouse coaching_events) value enums ────────────────────────


class CardType(str, enum.Enum):
    """Card surface types for coaching telemetry."""

    INFO = "info"
    ACTION = "action"
    QUIZ = "quiz"
    OBSERVATION = "observation"
    UNKNOWN = "unknown"


class TriggerType(str, enum.Enum):
    """What caused this telemetry event.

    Two axes mixed for backward compatibility:
    - rule strength + surface (`hard`, `soft`, `morning`) — used by the
      legacy v3.0 scenario flow when a card was surfaced
    - causal source (`gap`, `workflow_event`, `user_action`) — used by
      v3.3 module events and clinical-observation events
    """

    HARD = "hard"
    SOFT = "soft"
    MORNING = "morning"
    GAP = "gap"
    WORKFLOW_EVENT = "workflow_event"
    USER_ACTION = "user_action"
    UNKNOWN = "unknown"


class InferenceMode(str, enum.Enum):
    """Where the model output came from."""

    ONLINE = "online"
    EDGE = "edge"
    CACHED = "cached"
    UNKNOWN = "unknown"


class Outcome(str, enum.Enum):
    """Outcome values used by quiz/gap-profile ingestion."""

    CORRECT = "correct"
    WRONG = "wrong"
    INCORRECT = "incorrect"
    SKIP = "skip"
    UNKNOWN = "unknown"


class ValidatorStatus(str, enum.Enum):
    """Validator result for generated cards (see platform card_validator)."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    FALLBACK = "fallback"
    UNKNOWN = "unknown"

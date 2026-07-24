-- Standalone schema for SQLAlchemy models in:
-- services/platform/src/platform_service/db/models/
--
-- Target DB: PostgreSQL (uses UUID/JSONB/arrays/range + pgvector).
-- Safe to run on an empty database. Squashed snapshot of alembic head (0032).
-- Locale-keyed JSONB maps (*_localized) replace legacy *_bn/*_en columns (0030).
-- Includes chw_module_assignment (0022) and module_trigger_binding keyed by module_id.
-- Includes module lifecycle admin audit log (0031) and chw_training_request (0032).
-- Seed sections: config_threshold learning-points (0005), referral behavioural_gap
-- (0014), assessment-due trigger_definition (0026).

BEGIN;

-- Needed for gen_random_uuid() defaults (convenient for standalone SQL).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Needed for the Module.embedding column.
CREATE EXTENSION IF NOT EXISTS vector;

-- ──────────────────────────────────────────────────────────────────────────────
-- Source layer
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS source_document (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_document_family_id uuid NOT NULL DEFAULT gen_random_uuid(),
  title text NOT NULL,
  source_type text NOT NULL,
  primary_language text NOT NULL,
  content_domain text NOT NULL DEFAULT 'clinical',
  assessment_mode text NOT NULL DEFAULT 'with_quiz',
  version_label text NULL,
  publication_date date NULL,
  original_storage_path text NOT NULL,
  thumbnail_storage_path text NULL,
  content_sha256 text NULL,
  original_filename text NULL,
  uploaded_by text NULL,
  outline_method text NULL,
  outline_jsonb jsonb NULL,
  extraction_calibration_jsonb jsonb NULL,
  ingestion_instructions text NULL,
  sync_published_visible boolean NOT NULL,
  status text NOT NULL DEFAULT 'ingesting',
  ingested_at timestamptz NOT NULL DEFAULT now(),
  ingested_by uuid NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_source_document_content_sha256_ingested
  ON source_document (content_sha256)
  WHERE status = 'ingested' AND content_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS source_page (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
  page_number integer NOT NULL,
  start_ms integer NULL,
  end_ms integer NULL,
  page_image_path text NULL,
  markdown_content text NOT NULL DEFAULT '',
  extraction_method text NOT NULL DEFAULT 'text',
  extraction_quality_score double precision NOT NULL DEFAULT 0.0,
  text_extraction_alt text NULL,
  language_detected text NULL,
  metadata_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_source_page_doc_page UNIQUE (source_document_id, page_number)
);

CREATE TABLE IF NOT EXISTS content_block (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_page_id uuid NOT NULL REFERENCES source_page(id) ON DELETE CASCADE,
  block_order integer NOT NULL,
  block_type text NOT NULL,
  content_text text NOT NULL,
  content_language text NULL,
  heading_path_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_document_id uuid NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz NULL,
  status text NOT NULL DEFAULT 'running',
  error_jsonb jsonb NULL,
  triggered_by uuid NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_run_active_per_source
  ON ingestion_run (source_document_id)
  WHERE status = 'running'
    AND COALESCE(error_jsonb->>'type', '') != 'cross_source_fusion';

CREATE TABLE IF NOT EXISTS ingestion_run_step (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ingestion_run_id uuid NOT NULL REFERENCES ingestion_run(id) ON DELETE CASCADE,
  stage text NOT NULL,
  started_at timestamptz NULL,
  completed_at timestamptz NULL,
  status text NOT NULL DEFAULT 'pending',
  input_summary_jsonb jsonb NULL,
  output_summary_jsonb jsonb NULL,
  llm_call_id uuid NULL,
  error_jsonb jsonb NULL
);

CREATE TABLE IF NOT EXISTS llm_call_cache (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  input_hash text NOT NULL UNIQUE,
  model text NOT NULL,
  prompt_template_id uuid NULL,
  response_jsonb jsonb NOT NULL,
  token_usage_jsonb jsonb NULL,
  cached_at timestamptz NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Audit / provenance
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS file_upload (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bucket_name text NOT NULL,
  object_key text NOT NULL,
  storage_path text NOT NULL,
  original_filename text NOT NULL,
  content_sha256 text NOT NULL,
  content_type text NULL,
  size_bytes bigint NOT NULL,
  uploaded_by text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_file_upload_object UNIQUE (bucket_name, object_key)
);

CREATE INDEX IF NOT EXISTS ix_file_upload_storage_path ON file_upload (storage_path);
CREATE INDEX IF NOT EXISTS ix_file_upload_bucket_content_sha256
  ON file_upload (bucket_name, content_sha256);

CREATE TABLE IF NOT EXISTS attribution_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type text NOT NULL,
  source_document_id uuid NULL,
  module_id uuid NULL,
  actor text NOT NULL,
  payload_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_attribution_event_source_document
  ON attribution_event (source_document_id);
CREATE INDEX IF NOT EXISTS ix_attribution_event_event_type
  ON attribution_event (event_type);

CREATE TABLE IF NOT EXISTS module_candidate_draft (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ingestion_run_id uuid NOT NULL REFERENCES ingestion_run(id) ON DELETE CASCADE,
  proposed_title text NOT NULL,
  domain text NULL,
  behavioural_gap_code text NULL,
  scope_summary text NOT NULL DEFAULT '',
  description_localized jsonb NULL,
  source_provenance_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
  estimated_card_count integer NOT NULL DEFAULT 0,
  estimated_quiz_count integer NOT NULL DEFAULT 0,
  clinical_review_notes text NULL,
  proposed_module_type text NOT NULL DEFAULT 'refresher',
  previous_practice_summary text NULL,
  current_practice_summary text NULL,
  rationale_summary text NULL,
  ingestion_instruction_rationale text NULL,
  quality_flags_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Module layer
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS module_family (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_code text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by uuid NULL,
  current_published_module_id uuid NULL
);

-- NOTE: The ORM model does not declare a SQL-level FK for module_family_id.
-- This script intentionally mirrors that (so it stays in lockstep with models).
CREATE TABLE IF NOT EXISTS module (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_family_id uuid NOT NULL,
  version integer NOT NULL DEFAULT 1,
  title_localized jsonb NOT NULL,
  description_localized jsonb NULL,
  domain text NOT NULL,
  sub_domain text NULL,
  module_type text NOT NULL DEFAULT 'refresher',
  tenant_id uuid NULL,
  primary_gap_id uuid NULL,
  estimated_minutes integer NOT NULL DEFAULT 10,
  difficulty_level text NOT NULL DEFAULT 'moderate',
  source_document_ids uuid[] NULL,
  thumbnail_storage_path text NULL,
  urgent_publish boolean NOT NULL DEFAULT false,
  chatbot_faqs_only boolean NOT NULL DEFAULT false,
  module_json jsonb NULL,
  embedding vector NULL,
  visibility_window tstzrange NULL,
  pass_threshold_override double precision NULL,
  quality_flags_jsonb jsonb NULL,
  search_metadata_jsonb jsonb NULL,
  clinically_reviewed boolean NOT NULL DEFAULT false,
  clinically_reviewed_at timestamptz NULL,
  clinically_reviewed_by uuid NULL,
  lifecycle_status text NOT NULL DEFAULT 'draft',
  published_at timestamptz NULL,
  first_activated_at timestamptz NULL,
  last_deactivated_at timestamptz NULL,
  last_reactivated_at timestamptz NULL,
  deactivated_by uuid NULL,
  reactivated_by uuid NULL,
  deprecated_at timestamptz NULL,
  supersedes_module_id uuid NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_module_family_version UNIQUE (module_family_id, version)
);

CREATE TABLE IF NOT EXISTS module_quiz_question (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_id uuid NULL REFERENCES module(id) ON DELETE CASCADE,
  question_order integer NULL,
  question_family_id uuid NOT NULL DEFAULT gen_random_uuid(),
  question_version integer NOT NULL DEFAULT 1,
  case_setup_localized jsonb NULL,
  question_localized jsonb NOT NULL,
  question_type text NOT NULL DEFAULT 'single_select',
  options_localized jsonb NOT NULL,
  correct_indices integer[] NOT NULL,
  explanation_localized jsonb NULL,
  primary_card_family_id uuid NULL,
  source_block_ids uuid[] NULL,
  difficulty text NOT NULL DEFAULT 'moderate',
  distractor_critique_jsonb jsonb NULL,
  field_flags_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_module_quiz_family_version UNIQUE (question_family_id, question_version)
);

CREATE INDEX IF NOT EXISTS ix_module_quiz_question_module_id ON module_quiz_question (module_id);

CREATE TABLE IF NOT EXISTS module_card (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  card_order integer NOT NULL,
  card_family_id uuid NOT NULL DEFAULT gen_random_uuid(),
  card_version integer NOT NULL DEFAULT 1,
  title_localized jsonb NOT NULL,
  body_localized jsonb NULL,
  previous_practice_localized jsonb NULL,
  current_practice_localized jsonb NULL,
  rationale_for_change_localized jsonb NULL,
  next_action_localized jsonb NULL,
  thresholds_jsonb jsonb NULL,
  source_block_ids uuid[] NULL,
  figure_ref_block_id uuid NULL,
  search_metadata_jsonb jsonb NULL,
  attachments_jsonb jsonb NULL,
  field_flags_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_module_card_family_version UNIQUE (card_family_id, card_version)
);

CREATE INDEX IF NOT EXISTS ix_module_card_module_id ON module_card (module_id);

-- ──────────────────────────────────────────────────────────────────────────────
-- Gap + trigger layer
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS behavioural_gap (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gap_code text NOT NULL UNIQUE,
  description text NOT NULL,
  domain text NOT NULL,
  severity_default text NOT NULL DEFAULT 'moderate',
  detection_rule_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'active',
  tenant_id uuid NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS module_behavioural_gap (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  behavioural_gap_id uuid NOT NULL REFERENCES behavioural_gap(id) ON DELETE CASCADE,
  is_primary boolean NOT NULL DEFAULT false,
  CONSTRAINT uq_module_behavioural_gap_pair UNIQUE (module_id, behavioural_gap_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_module_behavioural_gap_one_primary
  ON module_behavioural_gap (module_id)
  WHERE is_primary;

CREATE INDEX IF NOT EXISTS ix_module_behavioural_gap_module_id
  ON module_behavioural_gap (module_id);

CREATE INDEX IF NOT EXISTS ix_module_behavioural_gap_behavioural_gap_id
  ON module_behavioural_gap (behavioural_gap_id);

CREATE TABLE IF NOT EXISTS chw_behavioural_gap_state (
  chw_id bigint NOT NULL,
  behavioural_gap_id uuid NOT NULL REFERENCES behavioural_gap(id) ON DELETE CASCADE,
  tenant_id uuid NULL,
  severity_current text NOT NULL DEFAULT 'moderate',
  first_observed_at timestamptz NULL,
  last_observed_at timestamptz NULL,
  last_reinforced_at timestamptz NULL,
  occurrence_count integer NOT NULL DEFAULT 0,
  failed_attempts_count integer NOT NULL DEFAULT 0,
  last_failed_attempt_at timestamptz NULL,
  escalated_to_supervisor boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'active',
  updated_at timestamptz NULL,
  CONSTRAINT pk_chw_behavioural_gap_state PRIMARY KEY (chw_id, behavioural_gap_id)
);

CREATE TABLE IF NOT EXISTS chw_module_completion (
  chw_id bigint NOT NULL,
  module_family_id uuid NOT NULL REFERENCES module_family(id) ON DELETE CASCADE,
  latest_completed_module_id uuid NULL,
  latest_attempt_module_id uuid NULL,
  completed_at timestamptz NULL,
  latest_attempt_at timestamptz NULL,
  latest_quiz_score double precision NULL,
  latest_attempt_passed boolean NOT NULL DEFAULT false,
  attempts_since_last_pass integer NOT NULL DEFAULT 0,
  reinforcement_due_at timestamptz NULL,
  tenant_id uuid NULL,
  CONSTRAINT pk_chw_module_completion PRIMARY KEY (chw_id, module_family_id)
);

CREATE TABLE IF NOT EXISTS chw_module_assignment (
  id uuid PRIMARY KEY,
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  assignment_type varchar(50) NOT NULL,
  tenant_id bigint NULL,
  user_id bigint NULL,
  upazila varchar(100) NULL,
  assigned_by bigint NOT NULL,
  assigned_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_module_assignment_tenant UNIQUE (module_id, tenant_id),
  CONSTRAINT uq_module_assignment_user UNIQUE (module_id, user_id),
  CONSTRAINT uq_module_assignment_upazila UNIQUE (module_id, upazila)
);

CREATE INDEX IF NOT EXISTS ix_chw_module_assignment_tenant_id
  ON chw_module_assignment (tenant_id);
CREATE INDEX IF NOT EXISTS ix_chw_module_assignment_user_id
  ON chw_module_assignment (user_id);
CREATE INDEX IF NOT EXISTS ix_chw_module_assignment_upazila
  ON chw_module_assignment (upazila);

CREATE TABLE IF NOT EXISTS chw_module_quiz_progress (
  chw_id bigint NOT NULL,
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  quiz_id uuid NOT NULL REFERENCES module_quiz_question(id) ON DELETE CASCADE,
  first_correct_at timestamptz NOT NULL DEFAULT now(),
  tenant_id uuid NULL,
  CONSTRAINT pk_chw_module_quiz_progress PRIMARY KEY (chw_id, module_id, quiz_id)
);

CREATE INDEX IF NOT EXISTS ix_chw_module_quiz_progress_chw_module
  ON chw_module_quiz_progress (chw_id, module_id);

CREATE TABLE IF NOT EXISTS chw_quiz_question_state (
  chw_id bigint NOT NULL,
  quiz_id uuid NOT NULL REFERENCES module_quiz_question(id) ON DELETE CASCADE,
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  tenant_id uuid NULL,
  failed_attempts_count integer NOT NULL DEFAULT 0,
  last_failed_attempt_at timestamptz NULL,
  first_attempt_at timestamptz NULL,
  last_attempt_at timestamptz NULL,
  escalated_to_supervisor boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'active',
  updated_at timestamptz NULL,
  CONSTRAINT pk_chw_quiz_question_state PRIMARY KEY (chw_id, quiz_id)
);

CREATE INDEX IF NOT EXISTS ix_chw_quiz_question_state_chw_module
  ON chw_quiz_question_state (chw_id, module_id);

CREATE TABLE IF NOT EXISTS chw_learning_point_event (
  event_id uuid NOT NULL,
  chw_id bigint NOT NULL,
  points integer NOT NULL,
  awarded_at timestamptz NOT NULL,
  tenant_id uuid NULL,
  CONSTRAINT pk_chw_learning_point_event PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS ix_chw_learning_point_event_chw_id ON chw_learning_point_event (chw_id);

CREATE TABLE IF NOT EXISTS chw_gap_telemetry_event (
  event_id uuid PRIMARY KEY,
  chw_id bigint NOT NULL,
  event_type text NOT NULL,
  processed_at timestamptz NOT NULL,
  tenant_id uuid NULL
);

CREATE TABLE IF NOT EXISTS trigger_definition (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trigger_kind text NOT NULL,
  trigger_code text NOT NULL UNIQUE,
  description text NULL,
  predicate_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
  predicate_schema_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active',
  tenant_id uuid NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS module_trigger_binding (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  trigger_definition_id uuid NOT NULL REFERENCES trigger_definition(id) ON DELETE CASCADE,
  relationship text NOT NULL DEFAULT 'primary',
  priority_weight integer NOT NULL DEFAULT 10,
  notes text NULL,
  CONSTRAINT uq_module_trigger_binding_pair UNIQUE (module_id, trigger_definition_id)
);

CREATE TABLE IF NOT EXISTS module_lifecycle_event (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  module_id uuid NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  event_type text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  actor_id uuid NULL,
  reason text NULL
);

CREATE INDEX IF NOT EXISTS ix_module_lifecycle_event_module_occurred
  ON module_lifecycle_event (module_id, occurred_at);

-- ──────────────────────────────────────────────────────────────────────────────
-- Chatbot FAQ layer
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_frequent_question (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  question_localized jsonb NOT NULL,
  normalized_question text NOT NULL,
  occurrence_count integer NOT NULL,
  rank integer NOT NULL,
  last_seen_at timestamptz NULL,
  computed_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_chat_faq_tenant_question UNIQUE (tenant_id, normalized_question)
);

CREATE INDEX IF NOT EXISTS ix_chat_faq_tenant_rank
  ON chat_frequent_question (tenant_id, rank);
CREATE INDEX IF NOT EXISTS ix_chat_faq_tenant_updated
  ON chat_frequent_question (tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_feedback_summary (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  payload_json jsonb NOT NULL,
  generated_at timestamptz NOT NULL,
  computed_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_feedback_summary_tenant
  ON chat_feedback_summary (tenant_id);

-- ──────────────────────────────────────────────────────────────────────────────
-- CHW training request
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chw_training_request (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  chw_id bigint NOT NULL,
  module_id uuid NULL REFERENCES module(id) ON DELETE RESTRICT,
  requested_module_name text NULL,
  reason text NULL,
  submitted_at timestamptz NOT NULL,
  tenant_id uuid NULL,
  reviewed_by text NULL,
  reviewed_at timestamptz NULL,
  reviewer_notes text NULL
);

CREATE INDEX IF NOT EXISTS ix_chw_training_request_chw_id
  ON chw_training_request (chw_id);

CREATE INDEX IF NOT EXISTS ix_chw_training_request_module_id
  ON chw_training_request (module_id);

CREATE INDEX IF NOT EXISTS ix_chw_training_request_tenant_submitted
  ON chw_training_request (tenant_id, submitted_at);

-- ──────────────────────────────────────────────────────────────────────────────
-- Config
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS config_threshold (
  id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  version integer NOT NULL DEFAULT 1,
  key text NOT NULL UNIQUE,
  value_json jsonb NOT NULL,
  title text NULL,
  description text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Learning-points config_threshold seed (migration 0005)
-- ──────────────────────────────────────────────────────────────────────────────

INSERT INTO config_threshold (version, key, value_json, title, description) VALUES
  (1, 'learning_points_module_delivered', '5'::jsonb,
   'Learning Points: Module Delivered',
   'CHW learning points awarded per module_delivered telemetry event'),
  (1, 'learning_points_module_card_viewed', '10'::jsonb,
   'Learning Points: Module Card Viewed',
   'CHW learning points awarded per module_card_viewed telemetry event'),
  (1, 'learning_points_module_quiz_attempted_base', '15'::jsonb,
   'Learning Points: Quiz Attempted (Base)',
   'Base CHW learning points for module_quiz_attempted (correct outcome)'),
  (1, 'learning_points_module_quiz_score_multiplier', '15'::jsonb,
   'Learning Points: Quiz Score Multiplier',
   'Quiz score bonus multiplier: floor(quiz_score_pct [0–1] * this) added to base'),
  (1, 'learning_points_module_completed', '20'::jsonb,
   'Learning Points: Module Completed',
   'CHW learning points awarded per module_completed telemetry event'),
  (1, 'learning_points_spice_action_observed', '3'::jsonb,
   'Learning Points: Spice Action Observed',
   'CHW learning points awarded per spice_action_observed telemetry event')
ON CONFLICT (key) DO NOTHING;

-- ──────────────────────────────────────────────────────────────────────────────
-- Referral behavioural_gap seed (seed/behavioural_gaps_referral.json; migration 0014)
-- ──────────────────────────────────────────────────────────────────────────────

INSERT INTO behavioural_gap (gap_code, description, domain, severity_default, detection_rule_jsonb, status)
VALUES
  ('referral_iccm_danger_signs', 'CHW did not follow ICCM general danger signs referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["General Danger Signs"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["General Danger Signs"]}]},"metadata":{"tier":"referred_reason","values":["General Danger Signs"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_iccm_respiratory', 'CHW did not follow ICCM respiratory referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Pneumonia","Cough"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Pneumonia","Cough"]}]},"metadata":{"tier":"referred_reason","values":["Pneumonia","Cough"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_iccm_fever_malaria', 'CHW did not follow ICCM fever/malaria referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Fever","Malaria"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Fever","Malaria"]}]},"metadata":{"tier":"referred_reason","values":["Fever","Malaria"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_iccm_diarrhoea', 'CHW did not follow ICCM diarrhoea referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Diarrhoea"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Diarrhoea"]}]},"metadata":{"tier":"referred_reason","values":["Diarrhoea"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_iccm_malnutrition', 'CHW did not follow ICCM malnutrition referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["MUAC"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["MUAC"]}]},"metadata":{"tier":"referred_reason","values":["MUAC"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_iccm_other_symptoms', 'CHW did not follow ICCM other-symptoms referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Symptoms"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Symptoms"]}]},"metadata":{"tier":"referred_reason","values":["Symptoms"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_rmnch_anc_high_risk', 'CHW did not follow ANC high-risk referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["High risk pregnant woman"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]}]},"metadata":{"tier":"referred_reason","values":["High risk pregnant woman"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_rmnch_anc_care_gaps', 'CHW did not follow ANC care-gap referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Gaps in ANC"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in ANC"]}]},"metadata":{"tier":"referred_reason","values":["Gaps in ANC"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_rmnch_pnc_mother_high_risk', 'CHW did not follow PNC mother high-risk referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["High risk mother"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]}]},"metadata":{"tier":"referred_reason","values":["High risk mother"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_rmnch_pnc_care_gaps', 'CHW did not follow PNC care-gap referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Gaps in PNC"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in PNC"]}]},"metadata":{"tier":"referred_reason","values":["Gaps in PNC"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_rmnch_childhood_visit', 'CHW did not follow childhood visit referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Childhood Visit Signs"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Childhood Visit Signs"]}]},"metadata":{"tier":"referred_reason","values":["Childhood Visit Signs"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_tb_symptoms', 'CHW did not follow TB symptoms referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["TB Symptoms"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["TB Symptoms"]}]},"metadata":{"tier":"referred_reason","values":["TB Symptoms"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_cbs', 'CHW did not follow CBS referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["CBS Referral"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["CBS Referral"]}]},"metadata":{"tier":"referred_reason","values":["CBS Referral"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_ncd_cardiometabolic', 'CHW did not follow NCD cardiometabolic referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["NCD","High BP","High BG"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["NCD","High BP","High BG"]}]},"metadata":{"tier":"referred_reason","values":["NCD","High BP","High BG"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_ncd_mental_health', 'CHW did not follow NCD mental-health referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Mental Health","Suicidal Ideation"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Mental Health","Suicidal Ideation"]}]},"metadata":{"tier":"referred_reason","values":["Mental Health","Suicidal Ideation"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_ncd_substance', 'CHW did not follow NCD substance-use referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Substance Abuse"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Substance Abuse"]}]},"metadata":{"tier":"referred_reason","values":["Substance Abuse"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_ncd_hiv_pregnancy', 'CHW did not follow NCD HIV/pregnancy-symptom referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["HIV","Pregnancy Symptoms"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["HIV","Pregnancy Symptoms"]}]},"metadata":{"tier":"referred_reason","values":["HIV","Pregnancy Symptoms"],"mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_family_planning_consult', 'CHW did not follow family planning consult recommendation after pregnancy outcome.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.referredReason","values":["Family Planning Consult"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Family Planning Consult"]}]},"metadata":{"tier":"referred_reason","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_anc_emergency_obstetric', 'CHW deviated from ANC emergency (obstetric) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["Suspected Pre-eclampsia","Abnormal fundal height","Urinary Bilirubin present"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["Suspected Pre-eclampsia","Abnormal fundal height","Urinary Bilirubin present"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"obstetric","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_emergency_acute', 'CHW deviated from ANC emergency (acute) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["High Fever","Abnormal Pulse","Abnormal weight gain"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["High Fever","Abnormal Pulse","Abnormal weight gain"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"acute","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_emergency_severe_anemia', 'CHW deviated from ANC emergency (severe_anemia) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["Severe Anemia"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["Severe Anemia"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"severe_anemia","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_emergency_chronic_untreated', 'CHW deviated from ANC emergency (chronic_untreated) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["PW not on treatment for existing chronic illnesses"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT","values":["PW not on treatment for existing chronic illnesses"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"chronic_untreated","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_non_emergency_demographic', 'CHW deviated from ANC non-emergency (demographic) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["High risk PW due to age/birth spacing"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["High risk PW due to age/birth spacing"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"demographic","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_non_emergency_anemia', 'CHW deviated from ANC non-emergency (anemia) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Moderate Anemia","Mild Anemia"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Moderate Anemia","Mild Anemia"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"anemia","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_non_emergency_diabetes', 'CHW deviated from ANC non-emergency (diabetes) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Suspected/Existing Case of Diabetes"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Suspected/Existing Case of Diabetes"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"diabetes","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_non_emergency_chronic_treated', 'CHW deviated from ANC non-emergency (chronic_treated) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["PW with existing chronic illnesses with treatment"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["PW with existing chronic illnesses with treatment"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"chronic_treated","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_non_emergency_other', 'CHW deviated from ANC non-emergency (other) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Mild Fever","H/O Preg related medical complications","Other Danger Signs"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT","values":["Mild Fever","H/O Preg related medical complications","Other Danger Signs"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk pregnant woman"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"other","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_anc_gap_supplementation', 'CHW deviated from ANC care-gap (supplementation) recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["Inadequate /Non consumption IFA","Inadequate /Non consumption Calcium","TT vaccination incomplete"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["Inadequate /Non consumption IFA","Inadequate /Non consumption Calcium","TT vaccination incomplete"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in ANC"]}]}]},"metadata":{"tier":"anc_gap","group":"supplementation","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_anc_gap_visit_cadence', 'CHW deviated from ANC care-gap (visit_cadence) recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["USG not done >36 weeks","ANC with Doctor not done >36 weeks","Less than 3 ANCs completed at end of 36 weeks"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["USG not done >36 weeks","ANC with Doctor not done >36 weeks","Less than 3 ANCs completed at end of 36 weeks"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in ANC"]}]}]},"metadata":{"tier":"anc_gap","group":"visit_cadence","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_anc_gap_delivery_plan', 'CHW deviated from ANC care-gap (delivery_plan) recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["Facility not identified for institutional delivery","Planned for Home Delivery"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.anc.summary.gapsInAnc","values":["Facility not identified for institutional delivery","Planned for Home Delivery"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in ANC"]}]}]},"metadata":{"tier":"anc_gap","group":"delivery_plan","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_pnc_emergency_bleeding_infection', 'CHW deviated from PNC emergency (bleeding_infection) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["Heavy bleeding","Foul-smelling discharge","Severe abdominal pain","Perineum tear / Discharge from wound area"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["Heavy bleeding","Foul-smelling discharge","Severe abdominal pain","Perineum tear / Discharge from wound area"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"bleeding_infection","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_emergency_neurological', 'CHW deviated from PNC emergency (neurological) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["Severe headache/visual issues/convulsions"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["Severe headache/visual issues/convulsions"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"neurological","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_emergency_hypertensive', 'CHW deviated from PNC emergency (hypertensive) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High BP","Not on treatment for HTN or Pre-eclampsia /Eclampsia","Edema","Urine Albumin"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High BP","Not on treatment for HTN or Pre-eclampsia /Eclampsia","Edema","Urine Albumin"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"hypertensive","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_emergency_metabolic', 'CHW deviated from PNC emergency (metabolic) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High Blood sugar","Known DM/GDM patient not on treatment","Suspected Jaundice"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High Blood sugar","Known DM/GDM patient not on treatment","Suspected Jaundice"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"metabolic","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_emergency_acute', 'CHW deviated from PNC emergency (acute) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High Fever","Abnormal Pulse","Severe Anemia"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.URGENT","values":["High Fever","Abnormal Pulse","Severe Anemia"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"acute","referral_type":"emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_non_emergency_anemia', 'CHW deviated from PNC non-emergency (anemia) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["Moderate Anemia","Mild Anemia"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["Moderate Anemia","Mild Anemia"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"anemia","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_non_emergency_breast', 'CHW deviated from PNC non-emergency (breast) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["Cracked nipples / painful / swollen breasts with or without fever"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["Cracked nipples / painful / swollen breasts with or without fever"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"breast","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_non_emergency_chronic_treated', 'CHW deviated from PNC non-emergency (chronic_treated) referral recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["On treatment for HTN or Pre-eclampsia / Eclampsia","On treatment for DM/GDM","Fever","Other Danger Signs"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT","values":["On treatment for HTN or Pre-eclampsia / Eclampsia","On treatment for DM/GDM","Fever","Other Danger Signs"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["High risk mother"]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]}]},"metadata":{"tier":"subcondition","group":"chronic_treated","referral_type":"non_emergency","mismatch_kind":"reason_or_urgency"}}'::jsonb, 'active'),
  ('referral_pnc_gap_supplementation', 'CHW deviated from PNC supplementation care-gap recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"array_contains_substring","path":"recommended.assessmentDetails.pncMother.pncGaps","value":"Supplementation"},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"array_contains_substring","path":"recommended.assessmentDetails.pncMother.pncGaps","value":"Supplementation"},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in PNC"]}]}]},"metadata":{"tier":"pnc_gap","group":"supplementation","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_pnc_gap_contraception', 'CHW deviated from PNC contraception care-gap recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.pncGaps","values":["Not using postpartum contraception"]},{"op":"or","conditions":[{"op":"and","conditions":[{"op":"contains_any","path":"recommended.assessmentDetails.pncMother.pncGaps","values":["Not using postpartum contraception"]},{"op":"missed_referral"}]},{"op":"mismatch_contains_any","recommended_path":"recommended.referredReason","actual_path":"actual.referralReasons","values":["Gaps in PNC"]}]}]},"metadata":{"tier":"pnc_gap","group":"contraception","mismatch_kind":"missed_or_wrong_reason"}}'::jsonb, 'active'),
  ('referral_type_emergency', 'CHW did not follow emergency (urgent) referral classification recommended by the rule engine.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"or","conditions":[{"op":"map_key_nonempty","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman","key":"URGENT"},{"op":"map_key_nonempty","path":"recommended.assessmentDetails.pncMother.motherRisks","key":"URGENT"}]},{"op":"mismatch_urgency","recommended_urgency":"URGENT","actual_path":"actual.isUrgent"}]},"metadata":{"tier":"referral_type","referral_type":"emergency","mismatch_kind":"urgency"}}'::jsonb, 'active'),
  ('referral_type_non_emergency', 'CHW did not follow non-emergency referral classification recommended by the rule engine.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"or","conditions":[{"op":"map_key_nonempty","path":"recommended.assessmentDetails.anc.summary.highRiskPregnantWoman","key":"NON_URGENT"},{"op":"map_key_nonempty","path":"recommended.assessmentDetails.pncMother.motherRisks","key":"NON_URGENT"}]},{"op":"mismatch_urgency","recommended_urgency":"NON_URGENT","actual_path":"actual.isUrgent"}]},"metadata":{"tier":"referral_type","referral_type":"non_emergency","mismatch_kind":"urgency"}}'::jsonb, 'active'),
  ('referral_location_upazila', 'CHW did not follow Upazila Health Complex destination recommended by the rule engine.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"or","conditions":[{"op":"eq","path":"recommended.referralFacilityType","value":"Upazila Health Complex"},{"op":"eq","path":"recommended.assessmentDetails.referralFacilityType","value":"Upazila Health Complex"}]},{"op":"or","conditions":[{"op":"missed_referral"},{"op":"mismatch_eq","recommended_path":"recommended.referralFacilityType","actual_path":"actual.destinationTier"},{"op":"mismatch_eq","recommended_path":"recommended.assessmentDetails.referralFacilityType","actual_path":"actual.destinationTier"}]}]},"metadata":{"tier":"location","destination":"upazila","mismatch_kind":"wrong_destination"}}'::jsonb, 'active'),
  ('referral_location_community_clinic', 'CHW did not follow Community Clinic destination recommended by the rule engine.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"or","conditions":[{"op":"eq","path":"recommended.referralFacilityType","value":"Community Clinic"},{"op":"eq","path":"recommended.assessmentDetails.referralFacilityType","value":"Community Clinic"}]},{"op":"or","conditions":[{"op":"missed_referral"},{"op":"mismatch_eq","recommended_path":"recommended.referralFacilityType","actual_path":"actual.destinationTier"},{"op":"mismatch_eq","recommended_path":"recommended.assessmentDetails.referralFacilityType","actual_path":"actual.destinationTier"}]}]},"metadata":{"tier":"location","destination":"community_clinic","mismatch_kind":"wrong_destination"}}'::jsonb, 'active'),
  ('referral_location_facility_selected', 'CHW did not follow rule-engine referral facility/site recommendation.', 'referral', 'high', '{"schema_version":1,"evaluator":"spice_referral_compliance","when":{"op":"and","conditions":[{"op":"or","conditions":[{"op":"exists","path":"recommended.assessmentDetails.anc.summary.referralFacility"},{"op":"exists","path":"recommended.assessmentDetails.pncMother.referralFacility"}]},{"op":"or","conditions":[{"op":"missed_referral"},{"op":"and","conditions":[{"op":"exists","path":"recommended.assessmentDetails.anc.summary.referralFacility"},{"op":"mismatch_eq","recommended_path":"recommended.assessmentDetails.anc.summary.referralFacility","actual_path":"actual.referredSiteId"}]},{"op":"and","conditions":[{"op":"exists","path":"recommended.assessmentDetails.pncMother.referralFacility"},{"op":"mismatch_eq","recommended_path":"recommended.assessmentDetails.pncMother.referralFacility","actual_path":"actual.referredSiteId"}]}]}]},"metadata":{"tier":"location","destination":"facility_selected","mismatch_kind":"wrong_site"}}'::jsonb, 'active')
ON CONFLICT (gap_code) DO UPDATE SET
  description = EXCLUDED.description,
  domain = EXCLUDED.domain,
  severity_default = EXCLUDED.severity_default,
  detection_rule_jsonb = EXCLUDED.detection_rule_jsonb,
  status = 'active',
  updated_at = now();

-- ──────────────────────────────────────────────────────────────────────────────
-- Assessment-due trigger_definition seed (seed/assessment_due_triggers.json; migration 0026)
-- ──────────────────────────────────────────────────────────────────────────────

INSERT INTO trigger_definition (trigger_kind, trigger_code, description, predicate_jsonb, predicate_schema_version, status)
VALUES
  ('workflow_event', 'wf:assessment_due:anc', 'Patients due for antenatal care visit', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"anc","match":{"encounter_type_any":["ANC"],"reason_any":[],"diagnosis_any":["ANC"],"patient_status_any":["anc"],"reason_display_any":["ANC Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["ANC"],"encounter_program_any":["RMNCH"],"is_pregnant":true,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:anemia', 'Anemia follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"anemia","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["ANEMIA"],"patient_status_any":["anemia"],"reason_display_any":["Anemia"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:cbs', 'CBS escalation follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"cbs","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["cbs"],"reason_display_any":["CBS"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:child_health', 'Under-five child health visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"child_health","match":{"encounter_type_any":["CHILDHOOD_VISIT","PNC_CHILD","PNC_NEONATE","UNDER_FIVE_YEARS","UNDER_TWO_MONTHS"],"reason_any":[],"diagnosis_any":["PNC_NEONATE","UNDER_FIVE_YEARS","UNDER_TWO_MONTHS"],"patient_status_any":["child_health"],"reason_display_any":["Childhood Visit Signs","PNC Neonate Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["CHILDHOOD_VISIT","PNC_CHILD","PNC_NEONATE"],"encounter_program_any":["CHILDHOOD_VISIT"],"is_pregnant":null,"max_age":5,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:childhood_visit', 'Childhood wellness visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"childhood_visit","match":{"encounter_type_any":["CHILDHOOD_VISIT"],"reason_any":[],"diagnosis_any":[],"patient_status_any":["childhood_visit"],"reason_display_any":["Childhood Visit Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["CHILDHOOD_VISIT"],"encounter_program_any":["CHILDHOOD_VISIT","RMNCH"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:cough', 'Cough follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"cough","match":{"encounter_type_any":[],"reason_any":["COUGH"],"diagnosis_any":[],"patient_status_any":["cough"],"reason_display_any":["Cough"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:diarrhea', 'Diarrhoea follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"diarrhea","match":{"encounter_type_any":["DIARRHEA"],"reason_any":["DIARRHEA","DIARRHOEA"],"diagnosis_any":["DIARRHEA","DIARRHOEA"],"patient_status_any":["diarrhea"],"reason_display_any":["Diarrhoea","Dysentry (Bloody Diarrhoea)","Watery diarrhoea / Dysentery"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:ear_problem', 'Ear problem follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"ear_problem","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["EARPROBLEM","EAR_PROBLEM"],"patient_status_any":["ear_problem"],"reason_display_any":["Ear Problem"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:fever', 'Fever follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"fever","match":{"encounter_type_any":[],"reason_any":["FEVER"],"diagnosis_any":[],"patient_status_any":["fever"],"reason_display_any":["Fever"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:general_danger_signs', 'General danger signs follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"general_danger_signs","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["general_danger_signs"],"reason_display_any":["General Danger Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:hiv_aids', 'HIV/AIDS follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"hiv_aids","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["HIVAIDS","HIVINFECTION","HIV_AIDS"],"patient_status_any":["hiv_aids"],"reason_display_any":["HIV Infection","HIV/AIDS"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:iccm', 'ICCM community follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"iccm","match":{"encounter_type_any":["DIARRHEA","ICCM","MALARIA","OTHER_SYMPTOMS","PNEUMONIA","UNDER_FIVE_YEARS","UNDER_TWO_MONTHS"],"reason_any":["COUGH","DIARRHEA","DIARRHOEA","FEVER","MALARIA","MUAC","PNEUMONIA","SYMPTOMS"],"diagnosis_any":["ANEMIA","EARPROBLEM","EAR_PROBLEM","HIVAIDS","HIVINFECTION","HIV_AIDS","JAUNDICE","MODERATEMALNUTRITION","MODERATE_MALNUTRITION","MUAC","OTHER_SYMPTOMS","SEVEREMALARIA","SEVEREMALNUTRITION","SEVERE_MALARIA","SEVERE_MALNUTRITION","UNCOMPLICATEDMALARIA","UNCOMPLICATED_MALARIA","UNDER_FIVE_YEARS","UNDER_TWO_MONTHS"],"patient_status_any":["iccm"],"reason_display_any":[],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:jaundice', 'Jaundice follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"jaundice","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["JAUNDICE"],"patient_status_any":["jaundice"],"reason_display_any":["Jaundice"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:malaria', 'Malaria follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"malaria","match":{"encounter_type_any":["MALARIA"],"reason_any":["MALARIA"],"diagnosis_any":["MALARIA","SEVEREMALARIA","SEVERE_MALARIA","UNCOMPLICATEDMALARIA","UNCOMPLICATED_MALARIA"],"patient_status_any":["malaria"],"reason_display_any":["Malaria","Uncomplicated Malaria"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:maternal_health', 'Maternal health follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"maternal_health","match":{"encounter_type_any":["ANC","PNC_MOTHER"],"reason_any":[],"diagnosis_any":["ANC","PNC"],"patient_status_any":["maternal_health"],"reason_display_any":["ANC Signs","Gaps in PNC","High Risk Mother","PNC Mother Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["ANC","PNC_MOTHER"],"encounter_program_any":["RMNCH"],"is_pregnant":true,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:miscarriage', 'Miscarriage follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"miscarriage","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["miscarriage"],"reason_display_any":["Miscarriage"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:moderate_malnutrition', 'Moderate malnutrition follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"moderate_malnutrition","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["MODERATEMALNUTRITION","MODERATE_MALNUTRITION"],"patient_status_any":["moderate_malnutrition"],"reason_display_any":["Moderate Malnutrition"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:muac', 'MUAC malnutrition follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"muac","match":{"encounter_type_any":[],"reason_any":["MUAC"],"diagnosis_any":["MUAC"],"patient_status_any":["muac"],"reason_display_any":["MUAC"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:ncd', 'NCD follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"ncd","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["ncd"],"reason_display_any":["NCD","NCDSymptoms"],"appointment_type_any":[],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:neonatal', 'Neonatal follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"neonatal","match":{"encounter_type_any":["PNC_NEONATE","UNDER_TWO_MONTHS"],"reason_any":[],"diagnosis_any":["PNC_NEONATE","UNDER_TWO_MONTHS"],"patient_status_any":["neonatal"],"reason_display_any":[],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["PNC_NEONATE"],"encounter_program_any":[],"is_pregnant":null,"max_age":0,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:on_treatment', 'On-treatment household visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"on_treatment","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["on_treatment"],"reason_display_any":["On Treatment","OnTreatment"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:other_symptoms', 'Other symptoms ICCM follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"other_symptoms","match":{"encounter_type_any":["OTHER_SYMPTOMS"],"reason_any":["SYMPTOMS"],"diagnosis_any":["OTHER_SYMPTOMS"],"patient_status_any":["other_symptoms"],"reason_display_any":["Symptoms","TB Symptoms"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:pnc_child', 'Postnatal child visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"pnc_child","match":{"encounter_type_any":["PNC_CHILD"],"reason_any":[],"diagnosis_any":[],"patient_status_any":["pnc_child"],"reason_display_any":["Childhood Visit Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["PNC_CHILD"],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:pnc_mother', 'Postnatal mother visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"pnc_mother","match":{"encounter_type_any":["PNC_MOTHER"],"reason_any":[],"diagnosis_any":["PNC"],"patient_status_any":["pnc_mother"],"reason_display_any":["Gaps in PNC","PNC Mother Signs","PNC Visit"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["PNC_MOTHER"],"encounter_program_any":["RMNCH"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:pnc_neonate', 'Neonatal postnatal visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"pnc_neonate","match":{"encounter_type_any":["PNC_NEONATE"],"reason_any":[],"diagnosis_any":["PNC_NEONATE"],"patient_status_any":["pnc_neonate"],"reason_display_any":["PNC Neonate Signs"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":["PNC_NEONATE"],"encounter_program_any":["RMNCH"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:pneumonia', 'Pneumonia follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"pneumonia","match":{"encounter_type_any":["PNEUMONIA"],"reason_any":["COUGH","PNEUMONIA"],"diagnosis_any":["PNEUMONIA"],"patient_status_any":["pneumonia"],"reason_display_any":["Cough or Difficult Breathing","Pneumonia","Pneumonia / Fever"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":["ICCM"],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:recovered', 'Recovered patient follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"recovered","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["recovered"],"reason_display_any":["Recovered"],"appointment_type_any":[],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:referred', 'Referred patient follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"referred","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["referred"],"reason_display_any":["Referred"],"appointment_type_any":["REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:respiratory', 'Respiratory illness follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"respiratory","match":{"encounter_type_any":["PNEUMONIA"],"reason_any":["COUGH","PNEUMONIA"],"diagnosis_any":["PNEUMONIA"],"patient_status_any":["respiratory"],"reason_display_any":[],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:severe_malaria', 'Severe malaria follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"severe_malaria","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["SEVEREMALARIA","SEVERE_MALARIA"],"patient_status_any":["severe_malaria"],"reason_display_any":["Severe Malaria"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:severe_malnutrition', 'Severe malnutrition follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"severe_malnutrition","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["SEVEREMALNUTRITION","SEVERE_MALNUTRITION"],"patient_status_any":["severe_malnutrition"],"reason_display_any":["Severe Malnutrition"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:symptoms', 'Unspecified symptoms follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"symptoms","match":{"encounter_type_any":[],"reason_any":["SYMPTOMS"],"diagnosis_any":[],"patient_status_any":["symptoms"],"reason_display_any":["Symptoms"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:tb_symptoms', 'TB symptoms follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"tb_symptoms","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":[],"patient_status_any":["tb_symptoms"],"reason_display_any":["TB Symptoms"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:under_five_years', 'Under-five-years ICCM visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"under_five_years","match":{"encounter_type_any":["UNDER_FIVE_YEARS"],"reason_any":[],"diagnosis_any":["UNDER_FIVE_YEARS"],"patient_status_any":["under_five_years"],"reason_display_any":[],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:under_two_months', 'Under-two-months ICCM visit due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"under_two_months","match":{"encounter_type_any":["UNDER_TWO_MONTHS"],"reason_any":[],"diagnosis_any":["UNDER_TWO_MONTHS"],"patient_status_any":["under_two_months"],"reason_display_any":[],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active'),
  ('workflow_event', 'wf:assessment_due:worsened', 'Worsened condition follow-up due today', '{"spice_event_code":"assessment_due","filter_predicate":{"assessment_topic":"worsened","match":{"encounter_type_any":[],"reason_any":[],"diagnosis_any":["WORSENED"],"patient_status_any":["worsened"],"reason_display_any":["Worsened"],"appointment_type_any":["HH_VISIT","MEDICAL_REVIEW","REFERRED"],"encounter_name_any":[],"encounter_program_any":[],"is_pregnant":null,"max_age":null,"min_age":null}}}'::jsonb, 1, 'active')
ON CONFLICT (trigger_code) DO UPDATE SET
  description = EXCLUDED.description,
  predicate_jsonb = EXCLUDED.predicate_jsonb,
  status = 'active',
  updated_at = now();

COMMIT;

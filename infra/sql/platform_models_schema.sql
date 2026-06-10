-- Standalone schema for SQLAlchemy models in:
-- services/platform/src/platform_service/db/models/
--
-- Target DB: PostgreSQL (uses UUID/JSONB/arrays/range + pgvector).
-- Safe to run on an empty database. Does not include migrations or triggers.

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
  primary_language text NOT NULL DEFAULT 'bn',
  content_domain text NOT NULL DEFAULT 'clinical',
  assessment_mode text NOT NULL DEFAULT 'with_quiz',
  authority_label text NOT NULL,
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
  status text NOT NULL DEFAULT 'ingesting',
  ingested_at timestamptz NOT NULL DEFAULT now(),
  ingested_by uuid NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

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
  behavioural_gap_code text NULL,
  scope_summary text NOT NULL DEFAULT '',
  description_en text NULL,
  description_bn text NULL,
  source_provenance_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
  estimated_card_count integer NOT NULL DEFAULT 0,
  estimated_quiz_count integer NOT NULL DEFAULT 0,
  clinical_review_notes text NULL,
  proposed_module_type text NOT NULL DEFAULT 'refresher',
  previous_practice_summary text NULL,
  current_practice_summary text NULL,
  rationale_summary text NULL,
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
  title_en text NULL,
  title_bn text NOT NULL,
  description_en text NULL,
  description_bn text NULL,
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
  module_json jsonb NULL,
  embedding vector NULL,
  visibility_window tstzrange NULL,
  pass_threshold_override double precision NULL,
  quality_flags_jsonb jsonb NULL,
  clinically_reviewed boolean NOT NULL DEFAULT false,
  clinically_reviewed_at timestamptz NULL,
  clinically_reviewed_by uuid NULL,
  lifecycle_status text NOT NULL DEFAULT 'draft',
  published_at timestamptz NULL,
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
  case_setup_en text NULL,
  case_setup_bn text NULL,
  question_en text NULL,
  question_bn text NOT NULL,
  question_type text NOT NULL DEFAULT 'single_select',
  options_en jsonb NULL,
  options_bn jsonb NOT NULL DEFAULT '[]'::jsonb,
  correct_indices integer[] NOT NULL,
  explanation_en text NULL,
  explanation_bn text NULL,
  primary_card_family_id uuid NULL,
  source_block_ids uuid[] NULL,
  difficulty text NOT NULL DEFAULT 'moderate',
  distractor_critique_jsonb jsonb NULL,
  field_flags_jsonb jsonb NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_module_quiz_family_version UNIQUE (question_family_id, question_version)
);

CREATE INDEX IF NOT EXISTS ix_module_quiz_question_module_id ON module_quiz_question (module_id);

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

CREATE TABLE IF NOT EXISTS chw_learning_point_event (
  event_id uuid NOT NULL,
  chw_id bigint NOT NULL,
  points integer NOT NULL,
  awarded_at timestamptz NOT NULL,
  tenant_id uuid NULL,
  CONSTRAINT pk_chw_learning_point_event PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS ix_chw_learning_point_event_chw_id ON chw_learning_point_event (chw_id);

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
  module_family_id uuid NOT NULL REFERENCES module_family(id) ON DELETE CASCADE,
  trigger_definition_id uuid NOT NULL REFERENCES trigger_definition(id) ON DELETE CASCADE,
  relationship text NOT NULL DEFAULT 'primary',
  priority_weight integer NOT NULL DEFAULT 10,
  notes text NULL,
  CONSTRAINT uq_module_trigger_binding_pair UNIQUE (module_family_id, trigger_definition_id)
);

-- ──────────────────────────────────────────────────────────────────────────────
-- Config
-- ──────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS config_threshold (
  id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  version integer NOT NULL DEFAULT 1,
  key text NOT NULL UNIQUE,
  value_json jsonb NOT NULL,
  description text NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

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
COMMIT;


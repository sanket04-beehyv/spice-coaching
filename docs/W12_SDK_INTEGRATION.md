# W-12 — SPICE SDK Integration

This doc is the single source of truth for how the MicroCoaching Android
SDK lives next to the SPICE 2.0 Android app. It supersedes
`docs/W12_AND_TEST_INFRA_DEFERRED.md`'s W-12 section, which described
this work when we still lacked SPICE repo access.

## The architecture model

The right framing — and the one this doc commits to — is:

> The MicroCoaching SDK is a **standalone Android library** the SPICE app
> embeds. It runs in-process with SPICE but is functionally independent:
> it owns its UI, its data, its decisions, its telemetry, and its
> on-device inference. SPICE does not hand us business logic, rule-engine
> output, or instrumentation work; SPICE hands us identifiers via
> lifecycle hooks and lets us paint into containers it provides.

That framing changes everything about what W-12 is. It is **not** a list
of changes to ask SPICE to make to their internal data models or
telemetry pipeline. It **is** a clear description of:

1. The integration surface SPICE consumes (a Gradle dep + a Builder
   call + a small set of lifecycle hooks + UI placement).
2. What our SDK already does today, mapped against the 11 placement-matrix
   slots — what's covered, what's stubbed, what's not built yet.
3. The data we actually need from SPICE (just identifiers — `chwId`,
   `patientId`, `encounterId`) and how we get it (lifecycle hooks SPICE
   calls; patient PII never crosses the boundary).
4. The genuine clarification asks — places where the SDK can't fully
   self-serve and SPICE has to confirm a hook signature, a screen
   transition, or a layout container.

## Three repos in play

| Repo | Role | Local path |
|---|---|---|
| `coaching-platform` (this repo) | Backend — content pipeline, sync API, telemetry ingest, admin dashboard. The SDK calls our endpoints. | `./` |
| `micro-coaching-android-sdk` | The Android library. Singleton SDK, on-device Gemma3 LLM, Room cache, OpenTelemetry, Compose UI. Already substantially built. | `~/medtronic-labs/micro-coaching-android-sdk` (`charles-updates` branch is the active head as of 2026-05-07) |
| SPICE 2.0 Android (`spice-2.0-android` `uhis-dev`) | Host app. Includes our SDK as a Gradle dep. | `https://github.com/Medtronic-LABS/spice-2.0-android` (read-only mirror at `/tmp/spice-research/spice-2.0-android` for this audit; commit `5043a97`) |

## Prerequisites for offline use

The SDK is offline-first by design — but "offline-first" means
"once initialised". Two prerequisites have to clear during a
window of connectivity before the SDK is fully operational:

1. **At least one successful W-9 sync.** A fresh device with no cached
   bundle cannot render scenario-driven coaching cards. The Room caches
   (`scenario_cache`, `quiz_question_cache`, `chw_gap_profile`,
   `learning_path`) are populated by `InboundSyncWorker` from our
   backend's `/sync/modules` endpoint. Until then, `CoachingDecisionEngine`
   returns `CoachingCardState.NoGuidanceAvailable`. After the first
   sync, all decision paths run from local data.

2. **EDGE mode requires a downloaded Gemma3 model (~800 MB).** Until
   the model lands, on-device chat falls back to ONLINE mode (which
   itself requires network). The model download is resumable via
   `ModelDownloadWorker`, so intermittent connectivity is tolerated,
   but a CHW with no realistic window of strong signal cannot use the
   chat surface at all. Card-rendering, telemetry, lifecycle hooks,
   and morning-brief selection do **not** depend on the model.

Once both clear, the runtime profile is:

| Capability | Network requirement |
|---|---|
| Lifecycle hook handling (decision-engine response) | none — in-process, runs on local Room |
| Morning brief selection (`MorningCardSelector`) | none — runs on `chw_gap_profile` cache |
| Card rendering | none — content from `scenario_cache` |
| On-device chat (EDGE mode) | none — Gemma3 runs on-device |
| Telemetry capture (`coaching_events` rows in Room) | none |
| Telemetry flush to backend | yes — `OutboundSyncWorker` retries via WorkManager exponential backoff |
| Module/scenario sync (W-9) | yes — `InboundSyncWorker` retries via WorkManager |
| Online card generation (ONLINE mode) | yes — graceful fallback to cached card on failure |

For battery and bandwidth: WorkManager syncs are gated by
`NetworkType.CONNECTED`; in poor-signal conditions retries run
exponentially backed off so the radio doesn't drain the battery.
A future optimisation worth flagging for SPICE's pilot ops team is
WiFi-only model downloads (the SDK already has a `wifiOnlyModelDownload`
config flag); first-time enrolment likely happens at a clinic with
WiFi anyway.

## Why we don't read `mdt_analytics_db` directly

The doc takes the position that SPICE pushes context into the SDK via
lifecycle hooks rather than letting the SDK read SPICE's local
analytics database. Four reasons, in priority order:

1. **The rule-engine decision isn't persisted to `mdt_analytics_db`.**
   SPICE's local analytics table records workflow events
   (`ScreeningCreation`, `NCDAssessmentCreation`, etc.) but the actual
   `referralStatus` + `referredReason` from the rule engine lives on
   `AssessmentEntity`, a different Room database. So a direct read
   wouldn't get us the data we actually need; it would just give us
   "an assessment happened" without the decision payload.

2. **Schema coupling.** Reading SPICE's Room DB makes our SDK depend
   on SPICE's column names and migration history. SPICE's schema
   evolves on their release cadence; lifecycle hook signatures evolve
   on a contract we both sign off.

3. **PII boundary is easier to audit at the SPICE call-site.** With
   hooks, SPICE explicitly chooses which fields to pass; we lint that
   list. With direct read, the SDK has unfiltered access to
   `HouseHoldMember.name` / `phoneNumber` / `nationalId`.

4. **Offline reliability is no worse, often better.** Lifecycle hooks
   fire in-process at the moment SPICE has the data — no network
   involved, no race with SPICE's own DB write. The SDK's Room
   persistence happens in the same coroutine, so even if SPICE's
   process crashes immediately after, the data is already ours.

The one case where direct read would help is event recovery if SPICE
fires a hook but our SDK process is somehow not initialised yet —
extremely narrow. Mitigation: `MicroCoachingInitializer` (AndroidX App
Startup) runs before any SPICE Activity, so the SDK is ready before
any hook can fire.

## What the SDK already does

We did not start from zero. The current SDK (`charles-updates`) ships:

| Component | File (`micro-coaching-android-sdk/sdk-android/src/main/java/com/medtroniclabs/microcoaching/`) | What it does |
|---|---|---|
| `MicroCoachingSDK` (singleton + Builder) | `MicroCoachingSDK.kt` | Init, config, lifecycle-hook entrypoints, public state flows |
| `MicroCoachingInitializer` (App Startup) | `MicroCoachingInitializer.kt` | Auto-init via AndroidX `Initializer` so SPICE doesn't even have to call Builder if defaults are fine |
| `MicroCoachingConfig` | `MicroCoachingConfig.kt` | Immutable config — backend URL, JWT, language, OTel endpoint, model path, demo-mode flag |
| `CoachingDecisionEngine` | `domain/decision/CoachingDecisionEngine.kt` | Picks a coaching card given a patient snapshot + scenario id + network state |
| `MorningCardSelector` | `domain/decision/MorningCardSelector.kt` | Selects morning briefing cards for `onHomeScreenShown` |
| `TelemetryManager` | `domain/telemetry/TelemetryManager.kt` | OpenTelemetry SDK init, span helpers, offline queue flush |
| `EventRecorder` | `domain/telemetry/EventRecorder.kt` | Records `coaching_events` rows the SDK posts to our backend's `/telemetry/events` |
| `OutputValidator` | `domain/validation/OutputValidator.kt` | Quality gate on LLM output before showing it |
| `ScenarioLookup` / `ScenarioRetriever` / `ScenarioRepository` | `data/cache/`, `data/repository/` | Local Room cache of synced scenarios (W-9 sync bundle) |
| `QuizRepository`, `LearningPathRepository`, `GapProfileRepository` | `data/repository/` | Local Room caches for the rest of the W-9 bundle |
| `CoachingFabView` | `ui/fab/CoachingFabView.kt` | A FAB that surfaces a coaching prompt — drops into any host layout |
| `CoachingCardFragment` | `ui/coaching/CoachingCardFragment.kt` | Renders a single coaching card; bound to `latestCardState` |
| `CoachingChatFragment` | `ui/chat/CoachingChatFragment.kt` | The on-device chat surface (Gemma3 streaming) |
| `CoachingFlowActivity` + `CoachingNavGraph` | `ui/flow/` | Full-screen flow for "go deeper" — quiz, learning path, etc. |
| `LearnFragment` | `ui/learn/LearnFragment.kt` | Explore / browse synced modules |
| `QuizQuestionScreen` + `QuizResultScreen` | `ui/quiz/` | Quiz UI (post-card knowledge check) |
| `ModelManager` + `ModelDownloadWorker` | `ai/` (or `services/`) | Gemma3 model download + state machine; resumable WorkManager job |
| `MicroCoachingDatabase` | `data/db/` | Separate Room DB; not shared with SPICE's Room |
| `MicroCoachingDataCallback` + `CoachingDataRepository` | `sdk/` | Public push + pull APIs the host can use to read SDK state |

The SDK exposes lifecycle hooks SPICE is expected to call — these are
the **only** integration points SPICE needs to wire:

```kotlin
// Already public on MicroCoachingSDK:
fun onHomeScreenShown(chwId: String)                                // implemented
fun onPatientSelected(patientId: String)                            // stub — Phase 1
fun onAssessmentSubmitted(encounterId: String, patientId: String,
                          assessmentData: Map<String, Any> = …)     // implemented
fun onRiskFlagSurfaced(patientId: String, riskLevel: String)        // stub — Phase 1
fun onCHWContextUpdated(chwWorkContext: CHWWorkContext)              // implemented
fun onConnectivityRestored()                                         // implemented
```

## Reference: what SPICE captures today

This section documents what the SPICE app stores and emits — the
ground-truth inventory we're integrating against. The SDK does not
read most of this directly (per the section above on `mdt_analytics_db`),
but designing lifecycle-hook payloads and gap-detection signals
requires knowing what's actually there.

### Referral capture

Two distinct write points; both are local-first with offline sync.

**1. Rule-engine output** (deterministic, pre-CHW-decision) —
`repo/AssessmentRepository.kt:44-95`. Persisted to Room as
`AssessmentEntity` immediately after the assessment form, with these
fields:

| Field | Source | Notes |
|---|---|---|
| `householdMemberLocalId`, `memberId`, `householdId`, `patientId`, `villageId` | derived | identifiers; the SDK never sees these directly |
| `assessmentType` | derived | menu id (ICCM, ANC, PNC, NCD, TB, etc.) |
| `assessmentDetails` | CHW + rule engine | full form-response JSON; **the triggering vital values live here as a JSON blob — not as dedicated columns** |
| `isReferred` | rule engine | true if any referral condition triggered |
| `referralStatus` | rule engine | enum: `Referred` / `OnTreatment` / `Recovered` / `Died` (`ui/assessment/referrallogic/utils/ReferralStatus.kt`) |
| `referredReason` | rule engine | `ArrayList<String>` (e.g. `["Pneumonia", "GeneralDangerSigns"]`) backed by `enum ReferralReasons` |
| `latitude`, `longitude` | device GPS | location of assessment |
| `followUpId`, `status` | CHW choice | follow-up linkage + custom status tags |

**2. CHW submission** (the actual decision) —
`ui/mypatients/repo/ReferPatientRepository.kt:52-85`. Persisted as
`ReferPatientResult` and synced to the SPICE backend via
`POST /spice-service/patient/referral-tickets/create`. Fields:

| Field | Source | Notes |
|---|---|---|
| `encounterId` | derived | assessment/encounter UUID |
| `referredReason` | **CHW free-text** | what the CHW typed in |
| `referredSiteId` | **CHW dropdown** | destination facility id |
| `referredClinicianId` | **CHW dropdown** | target clinician id |
| `provenance` | derived | `ProvanceDto` carrying `userId` (CHW), `organizationId`, `modifiedDate`, `spiceUserId` |
| `patientReference`, `patientId`, `householdId`, `villageId`, `memberId` | derived | identifiers |
| `assessmentName` | derived | workflow name (ANC / PNC / NCD / etc.) |
| `category`, `patientStatus`, `currentPatientStatus` | derived | always `REFERRED` here |

**Emergency vs Non-Emergency classification** is enumerated cleanly:

- `enum ANCUrgentReferrals` — `SUSPECTED_PRE_ECLAMPSIA`, `HIGH_FEVER`, `ABNORMAL_FUNDAL_HEIGHT`, `ABNORMAL_WEIGHT_GAIN`, `ABNORMAL_PULSE`, `SEVERE_ANEMIA`, `URINARY_BILIRUBIN`, `CHRONIC_ILLNESS_NOT_ON_TREATMENT`
- `enum ANCNonUrgentReferrals` — `HIGH_RISK_PREGNANCY`, `MODERATE_ANEMIA`, etc.
- `enum PNCUrgentReferrals` — `HEAVY_BLEEDING`, `SEVERE_ABDOMINAL_PAIN`, `SEVERE_HEADACHE_VISUAL_ISSUES_CONVULSIONS`, etc.

**Destination tier** for NCD is rule-driven — `FACILITY_TYPE_UPAZILA`
vs `FACILITY_TYPE_COMMUNITY_CLINIC`, computed from BP/BG severity in
`ReferralResultGenerator.computeReferralResultForBDNCD()`. Other
workflows let the CHW pick from a facility dropdown.

**Three real gaps** — none of these block W-12 (the SDK works
around them via lifecycle hooks), but they're worth flagging because
they shape what telemetry is achievable:

1. **No "did the CHW follow guidance" join key on the SPICE side.**
   `ReferPatientResult` carries the CHW's free-text `referredReason`
   but not the rule engine's recommendation. Joining
   `AssessmentEntity` ↔ `ReferPatientResult` on encounter id is
   workable but fragile. **The SDK works around this by capturing
   both signals independently** (rule-engine recommendation via
   `onScreeningCompleted` hook, CHW choice via `onReferralSubmitted`
   hook) and correlating on our backend.
2. **No referral-outcome status.** SPICE doesn't track "completed
   at the facility / no-show / declined." We can measure CHW
   *issuing* behaviour but not patient outcome. Out of scope for the
   pilot.
3. **Triggering vital values are buried in `assessmentDetails`
   JSON.** "Referred for High BP" exists; "BP was 162/108, threshold
   was 140/90" requires JSON-blob parsing. The SDK gets a clean view
   via `onVitalThresholdCrossed(vital, value, threshold)` (C4) at
   the point SPICE calculates the comparison, so this gap doesn't
   bite us.

### Other clinical events — the ~35 SPICE workflow events

SPICE has a custom analytics pipeline (Room → batched backend upload,
**not** Firebase) that emits almost every CHW workflow step. The
canonical event-name registry lives at
`Spice-SL/analytics/utils/AnalyticsDefinedParams.kt`. Sink:
local Room (`mdt_analytics_db`) → batched POST to
`spice-service/in-app-analytics/upload-file`.

**Common payload shape:**
`userId | role | eventType | parameter (JSON map) | lastSyncDate | createdAt`.

**The event catalog** (high-signal ones we'd want to know about,
even though we receive equivalents via lifecycle hooks):

| Class | Concrete events | What it tells us |
|---|---|---|
| Workflow completion | `ScreeningCreation`, `NCDAssessmentCreation`, `NCDInitialMedicalReviewCreation`, `NCDContinuousMedicalReviewCreation`, `NCDMedicalReviewSummaryCreation` | Did the CHW run the protocol step |
| Vitals capture | `NCDBloodPressureCreation` (+ `…ForNurse`), `NCDBloodGlucoseCreation` (+ `…ForNurse`) | Vitals taken or skipped |
| Counselling | `NCDLifestyleManagementCreation` / `…Updated` / `…Delete`, `NCDCounselorCreation` / `…Updated` / `…Delete` | Counselling delivered |
| Treatment | `NCDPrescriptionCreation` / `…Updated` / `…Delete`, `NCDTreatmentPlanCreation`, `NCDInvestigationCreation` / `…Update` / `…Delete`, `NCDInvestigationResultCreation` | Prescription, labs, results |
| Follow-up | `NCDScheduleCreation`, `NCDCallInitialed`, `NCDCallResult`, `NCDFollowUpFilter`, `NCDFollowUpSort` | Scheduling and outreach |
| Diagnosis + history | `NCDConfirmDiagnosisCreation`, `NCDPatientHistoryCreationForNCD` / `…ForMaternalHealth` / `…ForMentalHealth` | Clinical decision points |
| Lifecycle | `NCDPatientEdit`, `NCDPatientDelete`, `NCDPatientTransfer`, `NCDTransferStatus`, `NCDChangeFacility` | Patient lifecycle and handoffs |
| RMNCH | `RMNCHAssessment`, `PNCMOTHERASSESSMENT`, `RMNCHNeonateAssessment`, `RMNCHCHILDASSESSMENT`, `NCDInstructionPregnancyRisk`, `NCDUpdatePregnancyRisk` | Maternal/child workflow |
| Sync + UI engagement | `NCDUploadOfflineData`, `NCDSearchPatient`, `NCDPatientFilter`, `NCDPatientSort`, `NCDDashBoardCount`, `ReferralTicket` | Sync reliability and UI behaviour |

**The "events deleted after upload" wrinkle.** SPICE's `UploadWorker`
truncates `mdt_analytics_db` after a successful backend POST. So even
if we wanted to read `mdt_analytics_db` directly (we don't —
see "Why we don't read `mdt_analytics_db` directly" above), we'd
have to either subscribe to Room's `Flow<List<Analytics>>` for a
passive stream or add a `consumed_by_microcoaching` cursor column —
both intrusive. Lifecycle hooks dodge the wrinkle entirely: the
hook fires once, in-process, and the SDK persists what it needs.

### How this maps to our hook design

The lifecycle hooks (existing + the new ones in C2–C4) cover every
high-signal case the SPICE event catalog flags. Concrete mapping:

| SPICE event class | SDK lifecycle hook (existing or new) |
|---|---|
| `ScreeningCreation`, screening risk classification | new `onScreeningCompleted(encounterId, patientId, outcome)` (C3) |
| `NCDAssessmentCreation`, assessment submission with rule-engine outcome | existing `onAssessmentSubmitted(encounterId, patientId, assessmentData)` |
| `NCDBloodPressureCreation` / `NCDBloodGlucoseCreation` (post-threshold check) | new `onVitalThresholdCrossed(vital, value, threshold)` (C4) |
| `ReferralTicket` (referral submitted) | new `onReferralSubmitted(encounterId, patientId, referralContext)` (C2) |
| `NCDLifestyleManagementCreation`, `NCDCounselorCreation` (counselling done) | could surface via `onAssessmentSubmitted` / future `onCounsellingCompleted` (post-pilot) |
| `NCDCallInitialed`, `NCDCallResult` (follow-up done) | could surface via `onFollowUpCompleted` (post-pilot) |
| `RMNCHAssessment`, `PNCMOTHERASSESSMENT` etc. | `onAssessmentSubmitted` already covers, with `assessmentData` keys distinguishing pathway |
| Lifecycle / sync / UI engagement events | not needed for coaching decisions; the SDK has its own equivalents |

The pilot ships with the four existing hooks (`onHomeScreenShown`,
`onAssessmentSubmitted`, `onCHWContextUpdated`,
`onConnectivityRestored`) plus the four new ones in C2–C4 plus an
`onDashboardShown`. That's eight integration points total, covering
every coaching trigger in the placement matrix.

## The 11 slot matrix — what's covered, what's a gap

The placement matrix names 11 UI slots. For each, we check (a) which SDK
component renders the surface and (b) which lifecycle hook drives it.
A "Gap" row means the SDK doesn't have an implementation yet; this is
where W-12 work lands.

| # | Slot | SDK component (existing) | Driving hook | State |
|---|---|---|---|---|
| 1 | Morning Refresher (Home Screen / Landing) | `MorningCardSelector` → emits to `morningCards` flow → SPICE places a `CoachingCardFragment` or its own RecyclerView row that observes `morningCards` | `onHomeScreenShown(chwId)` ✅ | **covered** |
| 2 | In-between Visit (Home Screen / Landing) | None today | `onConnectivityRestored()` + an idle-window timer driven by SPICE's existing app-lifecycle | **gap — selector logic** |
| 3 | "What's New" Banner (Dashboard) | None today; the W-9 sync bundle delivers `module.visibility_window` so the data is present | new hook `onDashboardShown()` (or piggyback on `onHomeScreenShown`); SPICE places a `CoachingWhatsNewBanner` view | **gap — view + selector** |
| 4 | Inline Vitals Nudge (Screening / Assessment) | None today | new hook `onVitalEntered(vital, value)` → SDK decides whether to show a nudge; SPICE places an `InlineNudgeView` under the input | **gap — view + selector + hook** |
| 5 | BP "[?]" Help Byte | `CoachingFabView` is the right shape (small floating action) but currently configured for the FAB use case; would need a `CoachingHelpByteView` peer | new hook `onBPReadingStart()` (or simply a content key passed at view bind time) | **gap — view (small)** |
| 6 | Symptoms Modal Header Ribbon | None today | none needed — SPICE drops a `CoachingHeaderRibbon` view above the symptoms list with a content key like `"obstetric_symptoms_def"` | **gap — view (small)** |
| 7 | Referral Tier Guide | `CoachingFlowActivity` can be invoked for "go deeper" but the lightweight popup tier-guide is missing | new hook `onReferralFacilitySelectionShown()`; SPICE places a `CoachingTierGuidePopup` | **gap — view + selector** |
| 8 | Screening Summary Banner | `CoachingCardFragment` can render a banner-shaped card via a layout variant; the trigger (which content to show) needs the rule-engine outcome | new hook `onScreeningCompleted(outcome: ScreeningOutcome)` carrying minimal de-identified fields (risk level, primary trigger reason) | **gap — selector + hook** |
| 9 | Chart Tooltips (My Patients) | None today; chart library not yet identified in SPICE | depends on chart library — SPICE design lead has to pick the integration shape (XML overlay vs canvas-drawn) | **needs design** |
| 10 | Physical Exam Pro-Tip FAB (Medical Review) | `CoachingFabView` exists and renders. **Stub:** badge count is hardcoded to 1 (`CoachingFabView.kt:35` — "Phase 3: drive from LearnRepository") | new hook `onPhysicalExamStepShown(step)`; SPICE places `CoachingFabView` in the obstetric-exam fragment | **partial — view ok, hook + content keys + badge gap** |
| 11 | Sync Progress Modal | None today | new hook `onOfflineSyncShown()`; SPICE places a `CoachingSyncTipView` overlay | **gap — view + selector** |

Summary: **1 slot covered end-to-end** (slot 1), **1 partially covered** (slot 10 — view ok, needs hook + content keys + non-stubbed badge count), **1 needs SPICE design call** (slot 9), **8 need SDK-side work** (slots 2-8, 11). None of the gaps require SPICE to change their data model or instrument new analytics events. Every gap is satisfied by:

- a small new SDK view component (Compose-rendered inside an Android `View`-shaped wrapper so it drops into XML layouts), and/or
- a new selector function on `CoachingDecisionEngine`, and/or
- a new lifecycle hook on `MicroCoachingSDK` for SPICE to call.

## Data flow — three streams, no shared models

```
                             ┌─────────────────────────────┐
                             │ coaching-platform (backend) │
                             │                             │
                             │  - W-9 sync API             │
                             │  - W-10 telemetry ingest    │
                             │  - module pipeline          │
                             └────────────┬────────────────┘
                                          │  HTTPS + JWT
                  ┌───────────────────────┴──────────────────────────┐
                  │                                                  │
                  ▼ pull                                              ▲ push
            modules / cards /                                  coaching_events /
            scenarios / quizzes /                              digital_events
            learning paths
                  │                                                  │
                  │                                                  │
┌─────────────────┼──────────────────────────────────────────────────┼────────────────┐
│                 │   MicroCoaching SDK (in-process with SPICE)      │                │
│                 ▼                                                  │                │
│        ┌──────────────────┐    ┌─────────────────┐    ┌─────────────────────┐       │
│        │ Room cache       │    │ Decision engine │    │ TelemetryManager    │       │
│        │ (microcoaching_*) │───▶│ Morning…       │───▶│ + EventRecorder     │       │
│        │ separate from    │    │ Assessment…    │    │ → /telemetry/events │       │
│        │ SPICE's Room DB  │    │ Risk-flag…     │    │ → SigNoz OTel spans │       │
│        └──────────────────┘    └─────────────────┘    └─────────────────────┘       │
│                                         │                                           │
│                                         ▼ binds                                     │
│        ┌──────────────────────────────────────────────────────────────┐             │
│        │ UI components (CoachingCardFragment / FabView / ChatFragment │             │
│        │ / FlowActivity / FabView / Compose screens)                  │             │
│        └─────────────────────────────────┬────────────────────────────┘             │
│                                          ▲                                          │
└──────────────────────────────────────────┼──────────────────────────────────────────┘
                                           │ identifiers only (chwId, patientId,
                                           │ encounterId, optional assessmentData
                                           │ map of de-identified vitals)
                                           │
                              ┌────────────┴────────────────┐
                              │ SPICE 2.0 Android (host)    │
                              │ calls SDK lifecycle hooks   │
                              │ at known points; places SDK │
                              │ UI components in layouts    │
                              └─────────────────────────────┘
```

Three independent streams cross the SDK boundary:

1. **From our backend → SDK** (HTTPS, JWT auth): module + scenario + quiz +
   learning path bundles via the W-9 sync API. The SDK caches these locally
   in its own Room DB.
2. **From SPICE → SDK** (in-process function calls): identifiers + small
   maps of de-identified clinical signals via the lifecycle hooks. **No
   `HouseHoldMember`, no `name`, no `phoneNumber`, no `nationalId`.**
3. **From SDK → our backend** (HTTPS, JWT auth): coaching events posted
   to W-10's `/telemetry/events`; OpenTelemetry spans posted to a
   SigNoz/Grafana OTLP endpoint.

The boundary contract is: **SPICE never sees SDK internals; SDK never
sees SPICE PII**. The shape of every cross-boundary value is documented
on the lifecycle hook signature (e.g.
`assessmentData: Map<String, Any>` with a documented allowlist of keys
like `avg_systolic`, `bmi`, `cvd_risk_level`).

## What we get from each side — concrete

| Need | Where it comes from | How |
|---|---|---|
| Module content (cards, quizzes, learning paths) | coaching-platform W-9 `/sync/modules` | SDK `WorkManager` job pulls the bundle on a schedule + on connectivity restore |
| Scenario content + decision rules | coaching-platform sync bundle (already includes `scenario_cache`, `chw_gap_profile`, etc.) | Same sync job |
| Telemetry sink schema | coaching-platform W-10 `/telemetry/events` (`TelemetryEvent` / `TelemetryBatch` contracts in `mc_contracts.telemetry`) | Already defined on the chw-telemetry branch; SDK conforms |
| Patient context for decision-making | SPICE → SDK lifecycle hook | `onAssessmentSubmitted(encounterId, patientId, assessmentData)`, `onPatientSelected(patientId)`, `onRiskFlagSurfaced(patientId, riskLevel)` |
| Workflow timing (when to surface morning brief / sync banner) | SPICE → SDK lifecycle hook | `onHomeScreenShown(chwId)`, `onConnectivityRestored()` |
| Has the CHW made a referral? (clinical-outcome telemetry) | SPICE calls `onReferralSubmitted(encounterId, patientId, referralContext)` after the referral is locally persisted; the SDK records this as a `coaching_events` row of type `REFERRAL_OBSERVED` and posts it via the W-10 telemetry pipeline | New hook (small) — see "Genuine clarification asks" below (C2) |
| Was the referral correct? (compliance signal) | The backend computes compliance from **the SDK's own coaching_events stream**: pair the `MODULE_DELIVERED` event for the screening-summary banner (which carries the rule-engine `referredStatus` + `referredReason` we showed the CHW) against the subsequent `REFERRAL_OBSERVED` event (which carries what the CHW actually picked) on the same `(patient_id_hash, encounter_id)`. **There is no ingestion path from SPICE's referral DB into our backend; the entire compliance signal flows through the SDK.** This is why C2 + C3 are load-bearing — without those hooks, the backend has half the signal and can't compute compliance. |

## Where SPICE stays untouched

We deliberately do **not** ask SPICE to:

- Add fields to `ReferPatientResult`. Our backend correlates instead.
- Add a `consumed_by_microcoaching` cursor to `mdt_analytics_db`. We don't read that DB; we receive what we need via lifecycle hooks.
- Emit a `RuleEngineDecision` analytics event. The SDK observes the rule-engine outcome via the lifecycle hook payload (e.g. `onScreeningCompleted(outcome)`).
- Define a `MicroCoachingPatientSnapshot` projection. The PII filter lives at the SPICE call-site that builds the lifecycle-hook argument map.
- Modify any Room schemas, business logic, or rule-engine code.

The SPICE team's only integration tasks are:

1. Add our gradle dependency.
2. Call `MicroCoachingSDK.Builder(this).…build()` once in `Application.onCreate()`.
3. Place our UI components (`CoachingFabView`, `CoachingCardFragment`,
   `CoachingChatFragment`, etc.) into chosen layout containers.
4. Call our lifecycle hooks at known points (`onHomeScreenShown`,
   `onAssessmentSubmitted`, etc.).

That's the whole surface. Estimated SPICE-side effort: a single PR,
**~1 working day**, mostly because the lifecycle-hook call sites are
spread across several screens (home, screening, assessment, referral,
medical review, sync) and each one needs to be reached at exactly
the right point in the existing logic. The gradle wiring + Builder
call is fifteen minutes; the rest is finding the right line in each
of ~8 fragments / activities and confirming the data passed in is
de-identified.

## Genuine clarification asks

This is the small set of questions where the SDK genuinely cannot
self-serve. Each is a clarification, not a change-request — SPICE
confirms a callback signature or a screen transition; nothing about
their internal logic changes.

| # | Ask | Why it can't be inferred from the SPICE code we audited |
|---|---|---|
| C1 | **Confirm the lifecycle hook signature for `onAssessmentSubmitted`'s `assessmentData` map.** Today the SDK's docstring lists keys like `avg_systolic`, `bmi`, `cvd_risk_level` as "TEAM-CONFIRM". We need the SPICE Android lead to confirm which de-identified keys SPICE will pass and what their value types are. | SPICE has the data internally (`AssessmentEntity.assessmentDetails` JSON); the question is purely "which subset will you marshal at the call site". |
| C2 | **Confirm the lifecycle hook for referral submission.** Proposed: `onReferralSubmitted(encounterId, patientId, referralContext: Map<String, Any>)` carrying a documented allowlist (`isUrgent: Bool`, `destinationTier: String`, `referralReasons: List<String>`, `triggeringRuleEngineDecision: String?`). SPICE adds one line in `ReferPatientRepository.createReferPatientResult` after the local persist returns. | Without this hook the SDK must read `mdt_analytics_db` directly (cross-module DB read smell + still no rule-engine decision available there). One line at the call site is the cleaner integration. |
| C3 | **Confirm the lifecycle hook for screening completion** (`onScreeningCompleted(encounterId, patientId, outcome)`) carrying the rule-engine `referralStatus` enum + `referredReason: List<String>`. Same shape as C2. | Drives slot 8 (Screening Summary Banner). The data exists on `AssessmentEntity` already — we just need a hook so we don't pull from SPICE's Room DB. |
| C4 | **Confirm the lifecycle hook for vital-threshold crossing** (`onVitalThresholdCrossed(vital: String, value: Double, threshold: Double)`). | Drives slots 4 + 5. SPICE already calculates this in `BloodPressureViewModel` and `GlucoseViewModel`; we need them to call our SDK after the calculation. |
| C5 | **Slot 9 (chart tooltips) — design call.** Which chart library does SPICE use? Are tooltips XML-rendered or canvas-drawn? Does the SDK own the tooltip surface or just supply text content? | Not findable from the code (chart library used in `mypatients/PatientInfoFragment`'s adapter wasn't conclusively identified). 30-min design conversation. |
| C6 | **JWT scope & refresh.** Confirm that the JWT SPICE already issues to its CHW user is acceptable as the SDK's bearer to `coaching-platform` endpoints. If not, we mint our own per-CHW JWT at SDK init. | The SDK README assumes "SPICE JWT" but the actual auth contract on our backend's W-9/W-10 endpoints needs to match. |
| C7 | **Telemetry deduplication.** SDK generates a unique `event_id` (UUID) per event and Room enforces PK uniqueness on the local cache. Backend dedups by `event_id` via Redis `SET NX` with 24h TTL. Dedup is **backend-enforced**, not jointly guaranteed; the SDK retries on transient failures and the backend absorbs duplicates. | "Verify, don't change" — but worth confirming the SDK's `event_id` UUIDs are stable across an OutboundSyncWorker retry (i.e. the worker reuses IDs rather than minting new ones on each attempt). |

These seven clarifications close the contract. None of them is a request
for SPICE to modify their schema, instrument new events into
`mdt_analytics_db`, or change their rule-engine code.

## The PII boundary, made explicit

| Field | Crosses into the SDK? |
|---|---|
| `chwId` | yes — needed for telemetry user attribution |
| `patientId` (`patientTrackId`) | yes — but the SDK SHA-256-hashes it before any backend call (see existing `MicroCoachingSDK.onAssessmentSubmitted` — patient_id is hashed before scenario lookup) |
| `encounterId` | yes — needed for telemetry trace correlation |
| Age, gender, vital values, risk flags | yes — via `assessmentData` map; documented allowlist of keys |
| `name` | **no** |
| `phoneNumber` | **no** |
| `nationalId` | **no** |
| `dateOfBirth` | **no** (age band derived at SPICE call site) |
| `address` / `village` | village_id yes (already on telemetry schema), free-text address no |

Enforcement is at the SPICE call-site that builds the lifecycle-hook
argument map. The SDK does not accept a `HouseHoldMember` and never sees
one. A detekt rule is recommended (lints any `HouseHoldMember` import
inside an SDK call's argument expression on the SPICE side); not a
hard blocker.

## Work plan

| Phase | Work | Where | Estimate |
|---|---|---|---|
| W-12.1 | Send the seven C1–C7 clarification asks to SPICE Android lead. | comms | 0.5 day |
| W-12.2 | Document the in-progress SDK Builder + lifecycle-hook contract in `docs/SDK_CONTRACT.md` so SPICE has the canonical reference. Includes the `assessmentData` allowlist and the JWT/auth contract. | this repo + sdk repo (docs only) | 0.5 day |
| W-12.3 | SDK side: implement the four new lifecycle hooks (C2 onReferralSubmitted, C3 onScreeningCompleted, C4 onVitalThresholdCrossed, plus a `onDashboardShown` for slot 3). All thin shims into `CoachingDecisionEngine`. | `micro-coaching-android-sdk` | 1 day |
| W-12.4 | SDK side: ship the missing UI components (slot 2 idle-window banner, slot 3 What's New banner, slot 4 inline nudge, slot 5 help byte, slot 6 header ribbon, slot 7 tier guide popup, slot 8 screening summary banner, slot 11 sync tip view). All Compose; wrapped in Android `View` for XML layout drop-in. | `micro-coaching-android-sdk` | 2 days |
| W-12.5 | SDK side: extend `CoachingDecisionEngine` selectors for the eight new slots — what content to show given the lifecycle-hook payload. | `micro-coaching-android-sdk` | 1 day |
| W-12.6 | SDK side: bilingual rendering smoke test on a real device — confirm Bangla cards render with correct font + line-break behaviour in each new view. | `micro-coaching-android-sdk` | 0.5 day |
| W-12.7 | Backend side: extend the W-10 telemetry contract with the new `event_type` values that drive compliance. At minimum: `REFERRAL_OBSERVED` (from C2), `SCREENING_OUTCOME_OBSERVED` (from C3), `VITAL_THRESHOLD_OBSERVED` (from C4). Add `mc_contracts.enums.CoachingEventType` entries, ClickHouse schema columns if needed, and the analytics queries that compute compliance per CHW per period. | this repo | 1 day |
| W-12.8 | Slot 9 design call (C5). Decide v1-or-defer. | sync | 30 min |
| W-12.9 | Pilot integration handoff: working integration in SPICE's debug build. Includes the gradle wiring, the Builder call, the seven lifecycle-hook call sites, and the eight UI placements. | joint | 0.5 day |

Total: **~6.5 working days us-side**, **~1 working day SPICE-side**.
SPICE work is contained in one PR they can merge whenever convenient.

The SDK and backend tracks parallelise — W-12.3, W-12.4, W-12.5 happen
in the SDK repo on its own branch; W-12.7 happens here on
`module-generation-changes` (or a peer). They reconverge at W-12.9.

## Open risks

These are flagged here so they don't go quiet between W-12 and pilot:

1. **Compliance signal is end-to-end SDK-mediated.** There is no
   independent ingestion path from SPICE's `ReferPatientResult` into
   our backend. If the SDK misses an event (process crash, hook not
   called, telemetry queue corrupt), we have no alternative source.
   The mitigation is robust queueing (already present via Room +
   WorkManager) and explicit `MicroCoachingInitializer` ordering so
   the SDK is ready before any SPICE Activity could fire a hook.
2. **Slot 9 (chart tooltips) is design-blocked.** If SPICE's chart
   library is real-time-only (no offline data path), this slot can't
   ship without rework. Recommend deferring to v2 unless a half-day
   of design work confirms otherwise.
3. **Fresh-device bootstrap is online-mandatory.** A SK in a remote
   village with a brand-new device cannot use the SDK until at least
   one successful sync. Pilot ops needs to plan first-issue events
   (clinic provisioning, WiFi-first model download) accordingly. Not
   a code issue, but worth flagging in the rollout plan.
4. **Battery cost on poor connectivity.** WorkManager backoff handles
   this acceptably today, but a CHW with chronic poor signal could
   see disproportionate battery drain from sync retries. Worth
   measuring in the pilot's first week and adding a network-quality
   adaptive throttle if it bites.
5. **JWT lifecycle on the SDK side.** The SDK accepts a
   SPICE-issued JWT at Builder time but doesn't currently observe
   token expiry / refresh. If SPICE issues short-lived JWTs, the SDK
   will start posting 401s on long-running offline-then-online
   transitions. C6 covers the static "is this acceptable" question;
   the dynamic refresh question is a follow-up if SPICE's JWTs are
   short-lived.

## What's out of scope

- Runtime LLM for multi-condition counselling synthesis. The architecture
  decision is that we don't pre-bake combination modules; runtime chat
  (`CoachingChatFragment` + Gemma3) handles synthesis on demand. Phase 2.
- Replacing SPICE's existing rule-based counselling drawer. SPICE owns
  that.
- Multi-tenant SDK packaging. The pilot is one tenant (BRAC). Multi-tenant
  is post-pilot.
- Authoring tooling for the dashboard. Already covered by the W-6 admin
  surface.

## Glossary

- **Slot** — a UI placement point in the SPICE app where a SDK component
  renders. Eleven defined; see the slot matrix.
- **Lifecycle hook** — a public function on `MicroCoachingSDK` that
  SPICE calls to give us context (`onAssessmentSubmitted`, etc.).
- **Behavioural unit** — one CHW-actionable situation. The unit of
  meaning for module identification (see `ARCHITECTURE_RESET.md`).
- **PII** — name, phone number, national ID, date of birth, free-text
  address. Never crosses the SDK boundary.
- **Sync bundle** — what the SDK pulls from W-9: modules, cards,
  scenarios, quizzes, learning paths, gap profiles, config thresholds.
- **`coaching_events`** — ClickHouse table on our backend; the SDK
  posts here via `/telemetry/events`.
- **`mdt_analytics_db`** — SPICE's local analytics database. Out of
  scope; the SDK doesn't read it.

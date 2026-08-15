# State Ownership Contract

## Purpose

This document is the authoritative field-level state ownership contract for
`code-agent` under Phase 4A (Milestone M28.5B). It establishes:

1. **Field-level lifecycle authority and projection mapping** across Temporal
   history, `TemporalTaskState`, and relational Postgres tables—distinguishing
   active in-flight authority from terminal relational projections.
2. **Lifecycle recovery rules** for activity replay, workflow restarts, and
   disaster recovery—distinguishing existing capabilities from target capabilities.
3. **A prioritized, prerequisite-gated reduction plan** to eliminate redundant
   serialization in `TemporalTaskState` without breaking activity idempotency,
   replay, or query consumers.
4. **Verification prerequisites and rollback safety** for all future state
   reduction slices.

---

## Architectural Principles

1. **Temporal owns durable coordination and execution truth:**
   - Lifecycle sequencing, activity dispatch, retries, timeouts, signal waits,
     cancellation, and DAG coordination live in Temporal history.
   - Transient execution state between activities is an activity checkpoint,
     not a permanent database record.

2. **Postgres owns query projections and durable domain knowledge:**
   - Tasks, worker runs, execution plans, timeline events, human interactions,
     session states, proposals, artifacts, and memory are stored in normalized
     relational tables.
   - Relational tables serve API endpoints, the operator dashboard, and
     auditing.

3. **`TemporalTaskState` is an intermediate activity checkpoint, not a permanent record:**
   - `temporal_task_states` exists solely to pass uncommitted `OrchestratorState`
     across activity boundaries during an active workflow run.
   - At terminal workflow completion (`COMPLETED`, `FAILED`, `CANCELLED`),
     `TemporalTaskState` is deleted on all terminal paths (including delivery,
     failure, escalation rejection, and initial approval rejection). Relational
     tables are the permanent record of completed work.

4. **Lifecycle awareness prevents premature state pruning:**
   - Many fields (e.g. `result`, `verification`, `review`) only become durable
     in relational tables (`worker_runs`, `artifacts`) at terminal delivery.
   - During active execution, `TemporalTaskState` remains their sole active
     authority until terminal outcome persistence or dedicated intermediate
     reconstructors are introduced.

---

## Section 1: Field-Level Lifecycle Authority Table

`OrchestratorState` defines 31 fields passed between workflow steps. Because
state authority transitions over the task lifecycle, the table below maps each
field to:

- **Active Authority (In-Flight):** The store owning the authoritative data
  while the workflow is executing between activities.
- **Intermediate Projection / Store:** Where intermediate state is written
  during `_persist_intermediate_state()`.
- **Terminal Authority:** The permanent relational store after terminal
  outcome persistence (`_persist_state()` / `_persist_execution_outcome()`).
- **Write Path(s):** Where each store is updated in code.
- **Read Consumers:** Which activities, APIs, or UI views consume the field.

| Field | Active Authority (In-Flight) | Intermediate Projection / Store | Terminal Authority | Write Path(s) | Read Consumers |
|---|---|---|---|---|---|
| `current_step` | Temporal History | `temporal_task_states.state`, `tasks.status`, `task_timeline_events` | `tasks.status`, `task_timeline_events` | `orchestrator/temporal/activities.py` (`_persist_intermediate_state`) | Workflow step dispatch, Dashboard status/timeline |
| `session` | `tasks` & `sessions` tables | `temporal_task_states.state` | `tasks` & `sessions` tables (`session_id`, `user_id`, `channel`) | Ingress handlers (`apps/api/routes/tasks.py`, `webhooks.py`, `telegram.py`) | Session continuity, `/sessions/{id}`, `/tasks/{id}`, Dashboard |
| `task` | `tasks` table & state constraints | `temporal_task_states.state`, `tasks.constraints` | `tasks` table (`id`, `repo_url`, `branch`, `task_text`, `constraints`, `budget_*`) | Ingress handlers, permission escalation in `activities.py` | Worker dispatch activities, API `/tasks/{id}`, Dashboard |
| `normalized_task_text` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral (derived from `tasks.task_text`) | `orchestrator/nodes/ingestion.py` (`ingest_task`) | Prompt generation, worker routing policy |
| `task_kind` | `temporal_task_states.state` | `temporal_task_states.state`, `task_timeline_events` payload | `task_timeline_events` payload | `orchestrator/nodes/ingestion.py` (`_classify_task_kind`) | Internal routing, planner selection (not in `tasks.task_spec`) |
| `task_plan` | Authoritative `task_timeline_events` (`TASK_PLANNED`) | Not persisted; excluded from `temporal_task_states` under Wave 3A | `task_timeline_events`, `execution_plans`, `execution_plan_nodes` | `orchestrator/nodes/ingestion.py` (`plan_task`), timeline events | DAG execution loop, Dashboard DAG view, `/tasks/{id}/plan` |
| `task_spec` | `tasks.task_spec` & `temporal_task_states.state` | `tasks.task_spec`, `temporal_task_states.state` | `tasks.task_spec` (JSONB) | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Capability grants, API `/tasks/{id}`, Dashboard |
| `decomposed_plan` | Authoritative `task_timeline_events` (`TASK_PLANNED`) & operational validation against `execution_plans` | Not persisted; excluded from `temporal_task_states` under Wave 3A | `task_timeline_events`, `execution_plans`, `execution_plan_nodes` | `orchestrator/graph.py` (`decompose_task`), timeline events | Temporal DAG scheduler (`select_next_node`, `run_decomposed_node`, `merge_node_wave`), Dashboard |
| `node_outcomes` | `execution_plan_nodes.merged_logical_activity_key` (relational marker authority) & `execution_plan_node_attempts.result_payload` (attempt payload authority) | `temporal_task_states.state` (Wave 3B.1 retained dual-write; pruned in 3B.2), `execution_plan_node_attempts` | `execution_plan_nodes`, `execution_plan_node_attempts` | `orchestrator/temporal/activities.py` (`merge_node_wave`), `ExecutionPlanRepository` | Wave merge loop, retry decisions, aggregate result generation |
| `current_node_id` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral in-memory/checkpoint-only state (no direct relational column) | Node selection / execution activities in `activities.py` (`run_decomposed_node`) | Node execution tracing & logs |
| `repo_profile` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral (recomputed if needed) | Discovery / planning activities in `orchestrator/` | Worker prompt context |
| `memory` | `temporal_task_states.state` | `temporal_task_states.state` | `memory_personal`, `memory_project`, `session_states` (original sources) | Loaded on ingest (`build_orchestrator_graph_input`) from memory repositories | Worker dispatch prompt assembly |
| `route` | `tasks` table & `temporal_task_states.state` | `tasks` (`chosen_worker`, `chosen_profile`, `runtime_mode`, `route_reason`), `temporal_task_states.state` | `tasks`, `worker_runs` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Worker dispatch, `/tasks/{id}`, Dashboard |
| `approval` | `tasks.constraints["approval"]` & `human_interactions` (`type=PERMISSION`) & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints`, `human_interactions` | `tasks.constraints["approval"]`, `human_interactions(type=PERMISSION)` | `orchestrator/execution_outcome_service.py` (`_apply_approval_constraints`), `HumanInteractionRepository` | API `/tasks/{id}/approve`, `/tasks/{id}/reject`, Dashboard HITL cards |
| `dispatch` | `temporal_task_states.state` | `temporal_task_states.state` | `worker_runs` (`worker_type`, `worker_profile`, `runtime_mode`, `runtime_manifest`) | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Sandbox runtime executor, Dashboard run details |
| `result` | `temporal_task_states.state` | `temporal_task_states.state` (not in `worker_runs` mid-flight) | `worker_runs` & `artifacts` (patches, diffs, logs, outputs) | `orchestrator/execution_outcome_service.py` (`_create_worker_run`, `_persist_artifacts_for_run`) | Completion loop (repair/delivery), API `/tasks/{id}/runs`, Dashboard |
| `verification` | `temporal_task_states.state` | `temporal_task_states.state` (not in `worker_runs` mid-flight) | `worker_runs.verifier_outcome` (JSONB) & `artifacts` | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Completion loop repair decisions, Dashboard Verification tab |
| `review` | `temporal_task_states.state` | `temporal_task_states.state` (not in `artifacts` mid-flight) | `artifacts` (`type="independent_review_result"`) & `worker_runs.artifact_index` | `orchestrator/execution_outcome_service.py` (`_persist_artifacts_for_run`) | Completion loop repair decisions, Dashboard Review tab |
| `friction_reports` | Ephemeral in Temporal (`state.result.friction_reports` retains worker friction) | Not persisted; excluded from `temporal_task_states` under Wave 2 | Ephemeral in Temporal (`persist_friction_proposals=False`); `proposals` (`type=REFLECTION`) when enabled in standalone graph | `orchestrator/execution_improvement_proposal_service.py` | API `/proposals`, Dashboard Proposals tab |
| `memory_to_persist` | Retained `state.result` (canonically derived via `_resolve_memory_to_persist()` at `persist_memory_node` entry boundary) | Not persisted; excluded from `temporal_task_states` under Wave 2 | `memory_personal`, `memory_project`, `memory_observations`, `memory_proposals` | Extracted during run; persisted post-commit via `ObservationMemoryBridge` | Read by `persist_memory` activity (`persist_memory_node -> _admit_memory_candidates`) |
| `progress_updates` | Ephemeral in-memory accumulation | Not persisted; activity-local / runtime accumulation only | Ephemeral (not stored relationally) | `_progress_update()` / state accumulation (separate from `_notify_progress()`) | Legacy state accumulation; product progress notification is independently emitted by `_notify_progress()`. Excluded from `temporal_task_states` under Wave 1. |
| `timeline_events` | `task_timeline_events` (relational table, read-through rehydrated in `_get_current_state`) | `task_timeline_events` (batch inserted) | `task_timeline_events` table | `orchestrator/execution_outcome_service.py` (`_persist_timeline_events`) | Dashboard/API, `OrchestratorBrain.previous_attempts_history`, graph retry routing, and `_has_event()` activity idempotency guards. Excluded from `temporal_task_states` under Wave 2. |
| `timeline_persisted_count` | `temporal_task_states.state` (reconciled from DB on read-through) | `temporal_task_states.state` | `TaskTimelineRepository.count_by_attempt` | `orchestrator/execution_outcome_service.py` (`_persist_timeline_events`), reconciled in `_get_current_state` | Current-attempt timeline list-offset deduplication cursor |
| `repair_handoff_requested` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state` | Checkpoint-only (no full terminal relational column; triggers manual handoff timeline event) | Completion loop decisions in `orchestrator/temporal/activities.py` | Delivery activity, manual handoff notifications |
| `completion_loop` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints` (partially: `*_repair_passes_used` counters only) | Checkpoint-only for full state (`phase`, `repair_source`, `summary`); `tasks.constraints` holds partial repair counters only | `orchestrator/execution_outcome_service.py` (`_apply_completion_control_constraints`), `activities.py` | Workflow completion loop routing, repair budget enforcement |
| `errors` | Ephemeral in-memory accumulation | Not persisted; excluded from `temporal_task_states` under Wave 2 | `tasks.last_error` & `task_timeline_events` (`TASK_FAILED`) | `orchestrator/temporal/activities.py` (`record_workflow_failure`) | API `/tasks/{id}`, Dashboard error banners |
| `attempt_count` | `tasks.attempt_count` & `temporal_task_states.state` | `tasks.attempt_count`, `temporal_task_states.state` | `tasks.attempt_count` column | `TaskRepository.increment_attempt_count` | Timeline attempt partitioning, retry limits, Dashboard attempt history |
| `session_state_update` | Regenerated at consumption (`summarize_result` / `_persist_rejected_session_state`) | Not persisted; excluded from `temporal_task_states` under Wave 2 | `session_states` table (`active_goal`, `decisions_made`, `identified_risks`, `files_touched`) | `orchestrator/temporal/activities.py` (`_persist_rejected_session_state`, outcome persistence) | Subsequent task memory context, API `/sessions/{id}`, Dashboard |
| `scout_phase` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints["scout_phase"]` | `proposals.metadata_payload` | Scout activities | Scout prompt selection, proposal metadata builder |
| `scout_phase_results` | Single-phase `state.result` in Temporal (multi-phase chaining deferred/unsupported) | Not persisted; excluded from `temporal_task_states` under Wave 2 | `proposals` (in `metadata_payload`) & `artifacts` | `orchestrator/execution_outcome_service.py` (`_merge_scout_phase_result`) | Final scout proposal synthesis |
| `fanout_disabled_for_remainder` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state` | Temporal History | `orchestrator/temporal/activities.py` (`merge_node_wave`) | Subsequent wave scheduling (forces serial execution) |

---

## Section 2: Recovery Rules

### 1. Temporal Activity Replay
- When a worker restarts or an activity fails, Temporal replays the workflow event history up to the point of failure.
- Completed activities are **not re-executed**; their recorded outputs are replayed deterministically from Temporal history.
- When an in-flight activity begins or retries, it calls `_get_current_state(task_id)`:
  - If a snapshot exists in `TemporalTaskState`, it parses the state, reconciles approval status (`task.constraints["approval"]`), rehydrates `timeline_events` from `TaskTimelineRepository.list_by_task()`, and sets `timeline_persisted_count` from `TaskTimelineRepository.count_by_attempt()`.
  - If no snapshot exists (e.g. initial workflow start or missing-snapshot recovery), `_load_submission_for_task()` performs **partial relational reconstruction** from `tasks`, `worker_runs`, `execution_plans`, `execution_plan_nodes`, `execution_plan_node_attempts`, and `task_timeline_events` (restoring task spec, decomposed plan, node outcomes, latest dispatch/result, attempt count, and timeline history).

### 2. Workflow Restart / Continue-As-New
- When a workflow is restarted or continued-as-new, durable progress must be reconstructable without relying on deleted in-memory state.
- Excluding fields from `TemporalTaskState` is safe only if:
  - The field is purely ephemeral to a single activity invocation (e.g. `progress_updates`), OR
  - The field is explicitly reconstructed from relational tables prior to downstream consumption (e.g. `timeline_events`), OR
  - The field is canonically derived or regenerated at consumption from retained core state (e.g. `memory_to_persist` from `state.result`, `session_state_update` from `summarize_result`), OR
  - The field is governed by an explicit discard policy or represents an unsupported/deferred Temporal execution path where downstream consumers rely solely on retained core state (e.g. `friction_reports` ephemeral discard, `scout_phase_results` single-phase Temporal execution).

### 3. Postgres-Only In-Flight Recovery: Current Reality vs. Target Architecture

It is critical to distinguish what the codebase currently supports from the target architecture:

- **Current Reality (Terminal Tasks):**
  - For completed, failed, or cancelled tasks, Postgres relational tables (`tasks`, `worker_runs`, `artifacts`, `execution_plans`, `execution_plan_nodes`, `execution_plan_node_attempts`, `task_timeline_events`, `proposals`, `session_states`, `human_interactions`) store 100% of the durable record. `TemporalTaskState` is deleted upon terminal completion on standard execution paths.

- **Current Reality (In-Flight Tasks):**
  - The no-snapshot fallback in `_get_current_state()` performs **partial relational reconstruction** from tasks, worker runs, execution-plan evidence, and timeline events (via `_load_submission_for_task()`). It is sufficient for selected state (latest worker dispatch, worker result, decomposed plan, node outcomes, relational timeline, attempt count, and task spec), but is **not sufficient for arbitrary mid-flight recovery**:
    - Verification and review records (`state.verification`, `state.review`) are not re-attached mid-turn until persisted.
    - Completion loop repair pass counters, phase state (`phase`), repair source, and summary remain incomplete in pure fallback.
    - Permission escalation strictly asserts that `TemporalTaskState` exists (`assert snapshot is not None`).
    - Ephemeral workflow coordination flags (`fanout_disabled_for_remainder`, `repair_handoff_requested`) are not persisted relationally.

- **Target Architecture (Prerequisite for Full State Pruning):**
  - Before eliminating core execution state from `TemporalTaskState`, dedicated relational reconstructors and completion-loop rehydrators must be completed and verified.

### 4. Terminal State Cleanup

- Standard terminal paths (`_persist_state()` upon successful delivery, `record_workflow_failure()`, permission escalation rejection in `_reject_permission_escalation()`, and initial approval rejection in `persist_rejected_session_state()`) explicitly invoke `TemporalTaskStateRepository.delete(task_id=task_id)`.
- All terminal outcomes guarantee that no orphan `temporal_task_states` rows remain attached to completed, failed, or cancelled tasks.

---

## Section 3: State Reduction Sequence (Waves 1-3)

### Wave 1: Zero-Dependency Ephemeral State (Completed)

#### 1. `progress_updates`
- **Previous Behavior:** Appended strings to `state.progress_updates` and serialized in `TemporalTaskState`.
- **Status:** **Completed (Wave 1)**
- **Implemented Changes:**
  - Added `progress_updates` to `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - Excluded from `_serialize_temporal_task_state()`.
  - Verified default fallback `progress_updates = []` on snapshot deserialization.

---

### Wave 2: Gated Low/Medium Complexity Fields (Completed)

#### 2. `friction_reports`
- **Status:** **Completed (Wave 2 Closeout)**
- **Architecture & Safety Policy:**
  - `friction_reports` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - **Explicit Temporal Discard Policy:** Verification-generated friction reports are intentionally ephemeral and discarded across activity handoffs. Worker-originated friction remains preserved inside `state.result.friction_reports`.
  - Temporal terminal outcome persistence operates with `persist_friction_proposals=False`, making snapshot retention unnecessary.
- **Risk Level:** **Low**
- **Size Impact:** Medium
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 3. `memory_to_persist`
- **Status:** **Completed (Wave 2 Closeout)**
- **Architecture & Implementation:**
  - `memory_to_persist` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - **Centralized Canonical Resolution:** `persist_memory_node` invokes `_resolve_memory_to_persist(state)` at its entry boundary. If `state.memory_to_persist` is empty, candidates are canonically mapped from the retained `state.result.memory_to_persist`.
  - Admission (`_admit_memory_candidates`), outcome counting (`_memory_admission_outcome`), OpenInference tracing (`_memory_persistence_span_input`), timeline payloads (`_memory_persisted_payload`), and response formatting all operate consistently on the resolved state.
  - Legacy snapshots containing `state.memory_to_persist` are prioritized directly, guaranteeing backward compatibility without double-admission.
- **Risk Level:** **Low to Medium**
- **Size Impact:** Low to Medium
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 4. `timeline_events`
- **Status:** **Completed (Wave 2)**
- **Architecture & Implementation:**
  - `timeline_events` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - `_get_current_state()` authoritatively rehydrates `state.timeline_events` as typed `TaskTimelineEventState` objects from `task_timeline_events` via `TaskTimelineRepository.list_by_task(task_id)`.
  - `_persist_timeline_events()` uses list-offset cursor indexing (`current_attempt_events[state.timeline_persisted_count:]`) decoupled from sequence numbers, eliminating collision bugs on legacy offset sequence numbers.
  - Task-wide `_has_event()` semantics and semantic consumers (`OrchestratorBrain.previous_attempts_history`, `_get_previously_failed_workers`, `_run_init_environment`) remain fully functional across attempt/lease increments and HITL resumes.
  - `deliver_result` idempotently deletes stale `TemporalTaskState` snapshots on duplicate completion execution to prevent crash-recovery orphan state leaks.
- **Size Impact:** **Very High** (reduces snapshot size by 50–80%).
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 5. `scout_phase_results`
- **Status:** **Completed (Wave 2 Closeout)**
- **Architecture & Implementation:**
  - `scout_phase_results` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - **Explicit Temporal Contract Definition:** Deep-scout multi-phase chaining (`transition_to_research_phase`) is explicitly deferred and not part of the supported Temporal completion loop. Temporal scout tasks run as single-phase executions where `state.result` is authoritative.
  - Terminal scout proposals and artifacts are extracted directly from `state.result.json_payload` and `state.result.artifacts` without depending on intermediate `scout_phase_results`.
- **Risk Level:** **Low to Medium**
- **Size Impact:** Medium to High
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 6. `session_state_update`
- **Status:** **Completed (Wave 2 Closeout)**
- **Architecture & Implementation:**
  - `session_state_update` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - **Regenerated-at-Consumption Policy:** In `deliver_result`, `summarize_result` regenerates the `SessionStateUpdate` in the same activity execution immediately before `_persist_state()` writes it to `SessionStateRepository`.
  - Initial approval and permission rejections similarly regenerate compact session state updates on-demand inside `_persist_rejected_session_state()`.
  - No snapshot boundary exists between generation and persistence.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 7. `errors`
- **Status:** **Completed (Wave 2 Closeout)**
- **Architecture & Implementation:**
  - `errors` is excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - **Terminal Error Projection:** Workflow failures are authoritatively projected to `tasks.last_error` and a `TASK_FAILED` timeline event via `record_workflow_failure()`. No Temporal activities or downstream queries consume intermediate `state.errors`.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

---

### Wave 3: Plan & Node Outcomes

#### 8. `task_plan`, `decomposed_plan`
- **Status:** **Completed (Wave 3A)**
- **Architecture & Implementation:**
  - `task_plan` and `decomposed_plan` are excluded from intermediate `TemporalTaskState` serialization via `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.
  - Authoritative plan contracts are restored directly from `TASK_PLANNED` timeline event payloads via `restore_task_plan_from_events()` and `restore_decomposed_plan_from_events()`.
  - `TaskPlan` faithfully preserves `depends_on=None` (sequential) vs `[]` (independent) semantics and planner metadata (`complexity_reason`, `node_kind`, `aggregation_role`, `execution_mode`, `parallel_safe`).
  - Pre-decomposition lifecycles (such as initial approval checkpoints) preserve `task_plan` while `decomposed_plan` remains cleanly `None`.
  - When decomposition is complete, relational validation against Postgres `execution_plans` and `execution_plan_nodes` validates immutable scheduler contracts (`node_id`, sequence, `goal`/`title`, `depends_on`, `task_spec`, `node_kind`, `aggregation_role`, `execution_mode`, `parallel_safe`) and fails closed on tampering or corruption.
  - All direct snapshot readers (`_get_current_state`, `_merge_v2_wave`, `request_permission_escalation`, `resolve_permission_escalation`, `persist_rejected_session_state`) route through `_rehydrate_dag_state()`.
- **Size Impact:** High (removes complete plan and node spec trees from snapshot).
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

#### 9. `node_outcomes`
- **Status:** **Wave 3B.1 Completed (Dual-Write); Wave 3B.2 Targeted (Pruning)**
- **Architecture & Implementation:**
  - **3B.1 Dual-Write Phase:** `node_outcomes` remains serialized in `TemporalTaskState` snapshots to ensure rolling deployment safety (new snapshot -> old worker compatibility).
  - **Relational Merge Marker Authority:** `execution_plan_nodes.merged_logical_activity_key` (introduced via schema-only migration `20260816_0049`) is established as the durable relational authority for which logical attempt reached parent state.
  - **Attempt Payload Authority:** `execution_plan_node_attempts.result_payload` is the immutable authority for worker attempts, correctly supporting retries without corruption.
  - **Parent-Generated Outcome Support:** `execution_plan_nodes.terminal_result_payload` is used only when `marker == latest_logical_activity_key` (supporting skips and fan-out synthetic missing evidence).
  - **Strict Fail-Closed Rehydration & Parity:** `restore_merged_node_outcomes()` reconstructs marker-confirmed outcomes and fails closed with `RuntimeError` if an active marker cannot be validated against durable evidence. Legacy snapshot bootstrapping validates full outcome and digest parity before setting markers.
  - **Crash-Gap Isolation:** `select_next_node` checks `node.latest_logical_activity_key and node.terminal_result_payload and node.latest_logical_activity_key != node.merged_logical_activity_key` to reliably distinguish unmerged worker results from parent-merged state across restarts.
  - **Wave 3B.2 Follow-up:** Will prune `node_outcomes` from `TemporalTaskState` serialization once 3B.1 is deployed.
- **Risk Level:** **Low**
- **Size Impact:** Medium in 3B.1 (dual-write established); High in 3B.2 (removes cumulative worker result trees from snapshots).
- **Rollback Path:** Additive re-inclusion in `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS`.

---

## Section 4: Prerequisites per Reduction Slice

To guarantee zero regressions when reducing `TemporalTaskState` serialization in future PRs, each reduction slice must satisfy these five gates:

1. **Temporal Replay Test:**
   - Execute complete Temporal workflow replay tests using recorded workflow histories (e.g. `tests/integration/test_temporal_runtime.py`, `tests/integration/test_temporal_fanout_replay.py`).
   - Verify that workflow history replays deterministically without non-determinism errors.

2. **Idempotency & Retry Guard Verification:**
   - Explicitly verify that activity retry/resume guards (`_has_event` or its replacement) remain fully functional on activity restarts and worker crash simulations.

3. **Projection Verification:**
   - Assert that authoritative relational tables (e.g. `task_timeline_events`, `proposals`, `session_states`, `memory_*`) receive complete, well-formed rows during normal execution.
   - Assert that API endpoints and dashboard queries continue returning expected data.

4. **Reconstruction Test:**
   - Add unit/integration tests verifying that `_get_current_state` (or dedicated repository reconstructors) properly reconstructs any required state fields from relational tables if an activity is restarted mid-workflow.

5. **Additive Rollback Guarantee:**
   - Serialization pruning changes must be backward-compatible: deserialization logic must use default values (`Field(default_factory=...)`) when reading older snapshots that still contain or omit the pruned fields.
   - Re-adding any pruned field to serialization remains a safe, instant rollback.

---

## Summary Matrix

| State Category | Fields | Active Authority (In-Flight) | Terminal Authority | Elimination Priority | Target State |
|---|---|---|---|---|---|
| **Coordination & Step** | `current_step`, `current_node_id`, `completion_loop`, `fanout_disabled_for_remainder`, `repair_handoff_requested` | Temporal History / State Checkpoint | Temporal History / `tasks.constraints` (partial) | Retain in Checkpoint | Compact activity control struct |
| **Ingress & Spec** | `session`, `task`, `task_spec`, `task_kind`, `normalized_task_text`, `repo_profile`, `memory`, `route`, `approval`, `attempt_count` | `tasks`, `sessions`, `human_interactions(type=PERMISSION)` / State Checkpoint | `tasks`, `sessions`, `human_interactions(type=PERMISSION)` | Retain in Checkpoint (reference IDs where feasible) | Read-through from `tasks` table |
| **Execution Outcomes** | `dispatch`, `result`, `verification`, `review` | `temporal_task_states.state` | `worker_runs`, `artifacts` | Retain in Checkpoint during active turn | Retain until terminal delivery; deleted on completion |
| **Ephemeral Notifications** | `progress_updates` | Ephemeral in-memory | Ephemeral (not stored relationally) | **Wave 1 — Completed** | **Excluded from snapshot payload**; runtime accumulation only |
| **Proposals & Friction** | `friction_reports` | Ephemeral in Temporal (`persist_friction_proposals=False`) | `proposals` (when enabled) / Ephemeral | **Wave 2 Closeout — Completed** | **Excluded from snapshot payload**; ephemeral discard policy in Temporal |
| **Timeline & Events** | `timeline_events`, `timeline_persisted_count` | `task_timeline_events` (Postgres, read-through rehydration) | `task_timeline_events` | **Wave 2 (`timeline_events` slice) — Completed** | **Excluded from snapshot payload**; authoritatively rehydrated on read-through |
| **Memory & Ephemeral Updates** | `memory_to_persist`, `scout_phase_results`, `session_state_update`, `errors` | Retained `state.result` (canonical derivation) / Regenerated at consumption / Ephemeral in-memory | `memory_*`, `proposals`, `session_states`, `tasks` | **Wave 2 Closeout — Completed** | **Excluded from snapshot payload**; canonical derivation & regenerated-at-consumption |
| **DAG Plan Models** | `task_plan`, `decomposed_plan` | `task_timeline_events(TASK_PLANNED)` / operational validation against `execution_plans` | `task_timeline_events`, `execution_plans`, `execution_plan_nodes` | **Wave 3A — Completed** | **Excluded from snapshot payload**; authoritatively restored from timeline events with relational projection validation |
| **Node Outcomes** | `node_outcomes` | `execution_plan_nodes.merged_logical_activity_key` & `execution_plan_node_attempts.result_payload` | `execution_plan_nodes`, `execution_plan_node_attempts` | **Wave 3B.1 — Completed**; **Wave 3B.2 — Targeted pruning** | **Relational authority active; snapshot dual-write retained**; authoritatively restored via `restore_merged_node_outcomes()` |

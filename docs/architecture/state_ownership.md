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
| `task_plan` | `temporal_task_states.state` & `execution_plans` | `temporal_task_states.state`, `execution_plans`, `execution_plan_nodes` | `execution_plans`, `execution_plan_nodes` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | DAG execution loop, Dashboard DAG view, `/tasks/{id}/plan` |
| `task_spec` | `tasks.task_spec` & `temporal_task_states.state` | `tasks.task_spec`, `temporal_task_states.state` | `tasks.task_spec` (JSONB) | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Capability grants, API `/tasks/{id}`, Dashboard |
| `decomposed_plan` | `temporal_task_states.state` | `temporal_task_states.state`, `execution_plans`, `execution_plan_nodes` | `execution_plans`, `execution_plan_nodes` | `orchestrator/execution_outcome_service.py`, `ExecutionPlanRepository` | Temporal DAG scheduler (`select_next_node`, `run_decomposed_node`, `merge_node_wave`), Dashboard |
| `node_outcomes` | `temporal_task_states.state` | `temporal_task_states.state`, `execution_plan_node_attempts` | `execution_plan_nodes`, `execution_plan_node_attempts` | `orchestrator/temporal/activities.py` (`merge_node_wave`), `ExecutionPlanRepository` | Wave merge loop, retry decisions, aggregate result generation |
| `current_node_id` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral in-memory/checkpoint-only state (no direct relational column) | Node selection / execution activities in `activities.py` (`run_decomposed_node`) | Node execution tracing & logs |
| `repo_profile` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral (recomputed if needed) | Discovery / planning activities in `orchestrator/` | Worker prompt context |
| `memory` | `temporal_task_states.state` | `temporal_task_states.state` | `memory_personal`, `memory_project`, `session_states` (original sources) | Loaded on ingest (`build_orchestrator_graph_input`) from memory repositories | Worker dispatch prompt assembly |
| `route` | `tasks` table & `temporal_task_states.state` | `tasks` (`chosen_worker`, `chosen_profile`, `runtime_mode`, `route_reason`), `temporal_task_states.state` | `tasks`, `worker_runs` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Worker dispatch, `/tasks/{id}`, Dashboard |
| `approval` | `tasks.constraints["approval"]` & `human_interactions` (`type=PERMISSION`) & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints`, `human_interactions` | `tasks.constraints["approval"]`, `human_interactions(type=PERMISSION)` | `orchestrator/execution_outcome_service.py` (`_apply_approval_constraints`), `HumanInteractionRepository` | API `/tasks/{id}/approve`, `/tasks/{id}/reject`, Dashboard HITL cards |
| `dispatch` | `temporal_task_states.state` | `temporal_task_states.state` | `worker_runs` (`worker_type`, `worker_profile`, `runtime_mode`, `runtime_manifest`) | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Sandbox runtime executor, Dashboard run details |
| `result` | `temporal_task_states.state` | `temporal_task_states.state` (not in `worker_runs` mid-flight) | `worker_runs` & `artifacts` (patches, diffs, logs, outputs) | `orchestrator/execution_outcome_service.py` (`_create_worker_run`, `_persist_artifacts_for_run`) | Completion loop (repair/delivery), API `/tasks/{id}/runs`, Dashboard |
| `verification` | `temporal_task_states.state` | `temporal_task_states.state` (not in `worker_runs` mid-flight) | `worker_runs.verifier_outcome` (JSONB) & `artifacts` | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Completion loop repair decisions, Dashboard Verification tab |
| `review` | `temporal_task_states.state` | `temporal_task_states.state` (not in `artifacts` mid-flight) | `artifacts` (`type="independent_review_result"`) & `worker_runs.artifact_index` | `orchestrator/execution_outcome_service.py` (`_persist_artifacts_for_run`) | Completion loop repair decisions, Dashboard Review tab |
| `friction_reports` | `temporal_task_states.state` | `temporal_task_states.state` | Ephemeral in Temporal (`persist_friction_proposals=False`); `proposals` (`type=REFLECTION`) when enabled | `orchestrator/execution_improvement_proposal_service.py` | API `/proposals`, Dashboard Proposals tab |
| `memory_to_persist` | `temporal_task_states.state` | `temporal_task_states.state` | `memory_personal`, `memory_project`, `memory_observations`, `memory_proposals` | Extracted during run; persisted post-commit via `ObservationMemoryBridge` | Read by `persist_memory` activity (`persist_memory_node -> _admit_memory_candidates`) |
| `progress_updates` | Ephemeral in-memory accumulation | Not persisted; activity-local / runtime accumulation only | Ephemeral (not stored relationally) | `_progress_update()` / state accumulation (separate from `_notify_progress()`) | Legacy state accumulation; product progress notification is independently emitted by `_notify_progress()`. Excluded from `temporal_task_states` under Wave 1. |
| `timeline_events` | `temporal_task_states.state` (active in-memory buffer) | `task_timeline_events` (batch inserted) & `temporal_task_states.state` | `task_timeline_events` table | `orchestrator/execution_outcome_service.py` (`_persist_timeline_events`) | Dashboard/API, **and** `_has_event()` activity idempotency guards |
| `timeline_persisted_count` | `temporal_task_states.state` | `temporal_task_states.state` | `TaskTimelineRepository.count_by_attempt` | `orchestrator/execution_outcome_service.py` (`_persist_intermediate_state`), reconciled in `_get_current_state` | Timeline batch insertion deduplication cursor |
| `repair_handoff_requested` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state` | Checkpoint-only (no full terminal relational column; triggers manual handoff timeline event) | Completion loop decisions in `orchestrator/temporal/activities.py` | Delivery activity, manual handoff notifications |
| `completion_loop` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints` (partially: `*_repair_passes_used` counters only) | Checkpoint-only for full state (`phase`, `repair_source`, `summary`); `tasks.constraints` holds partial repair counters only | `orchestrator/execution_outcome_service.py` (`_apply_completion_control_constraints`), `activities.py` | Workflow completion loop routing, repair budget enforcement |
| `errors` | `temporal_task_states.state` | `temporal_task_states.state`, `tasks.last_error` | `tasks.last_error` & `task_timeline_events` (`TASK_FAILED`) | `orchestrator/temporal/activities.py` (`record_workflow_failure`) | API `/tasks/{id}`, Dashboard error banners |
| `attempt_count` | `tasks.attempt_count` & `temporal_task_states.state` | `tasks.attempt_count`, `temporal_task_states.state` | `tasks.attempt_count` column | `TaskRepository.increment_attempt_count` | Timeline attempt partitioning, retry limits, Dashboard attempt history |
| `session_state_update` | `temporal_task_states.state` | `temporal_task_states.state` | `session_states` table (`active_goal`, `decisions_made`, `identified_risks`, `files_touched`) | `orchestrator/temporal/activities.py` (`_persist_rejected_session_state`, outcome persistence) | Subsequent task memory context, API `/sessions/{id}`, Dashboard |
| `scout_phase` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state`, `tasks.constraints["scout_phase"]` | `proposals.metadata_payload` | Scout activities | Scout prompt selection, proposal metadata builder |
| `scout_phase_results` | `temporal_task_states.state` | `temporal_task_states.state` | `proposals` (in `metadata_payload`) & `artifacts` | `orchestrator/execution_outcome_service.py` (`_merge_scout_phase_result`) | Final scout proposal synthesis |
| `fanout_disabled_for_remainder` | Temporal History & `temporal_task_states.state` | `temporal_task_states.state` | Temporal History | `orchestrator/temporal/activities.py` (`merge_node_wave`) | Subsequent wave scheduling (forces serial execution) |

---

## Section 2: Recovery Rules

### 1. Temporal Activity Replay
- When a worker restarts or an activity fails, Temporal replays the workflow event history up to the point of failure.
- Completed activities are **not re-executed**; their recorded outputs are replayed deterministically from Temporal history.
- When an in-flight activity begins or retries, it calls `_get_current_state(task_id)`:
  - If a snapshot exists in `TemporalTaskState`, it parses the state and reconciles approval status (`task.constraints["approval"]`) and timeline event count (`TaskTimelineRepository.count_by_attempt`).
  - If no snapshot exists (e.g. initial workflow start), it falls back to `_load_submission_for_task` + `build_orchestrator_graph_input()`, which reconstructs the **initial task input**, not mid-flight state.

### 2. Workflow Restart / Continue-As-New
- When a workflow is restarted or continued-as-new, durable progress must be reconstructable without relying on deleted in-memory state.
- Excluding fields from `TemporalTaskState` is safe only if:
  - The field is purely ephemeral to a single activity invocation (e.g. `progress_updates`), OR
  - The field is explicitly reconstructed from relational tables prior to downstream consumption.

### 3. Postgres-Only In-Flight Recovery: Current Reality vs. Target Architecture

It is critical to distinguish what the codebase currently supports from the target architecture:

- **Current Reality (Terminal Tasks):**
  - For completed, failed, or cancelled tasks, Postgres relational tables (`tasks`, `worker_runs`, `artifacts`, `execution_plans`, `execution_plan_nodes`, `execution_plan_node_attempts`, `task_timeline_events`, `proposals`, `session_states`, `human_interactions`) store 100% of the durable record. `TemporalTaskState` is deleted upon terminal completion on standard execution paths.

- **Current Reality (In-Flight Tasks):**
  - The missing-snapshot fallback in `_get_current_state()` only rebuilds **initial task input**.
  - Postgres relational tables **cannot currently reconstruct mid-flight state** if `TemporalTaskState` is lost while a task is running. If the snapshot is missing mid-flight:
    - Worker results, verification reports, and review outcomes are lost from active workflow memory.
    - Completion loop repair pass counters, phase state, repair source, and summary are lost.
    - Permission escalation strictly asserts that `TemporalTaskState` exists (`assert snapshot is not None`).
    - DAG wave execution loses in-memory node outcome aggregations.

- **Target Architecture (Prerequisite for Full State Pruning):**
  - Before eliminating core execution state from `TemporalTaskState`, dedicated relational reconstructors (e.g. loading `node_outcomes` from `execution_plan_node_attempts`, `decomposed_plan` from `execution_plan_nodes`, and worker results from `worker_runs`) must be implemented and tested.

### 4. Terminal State Cleanup

- Standard terminal paths (`_persist_state()` upon successful delivery, `record_workflow_failure()`, permission escalation rejection in `_reject_permission_escalation()`, and initial approval rejection in `persist_rejected_session_state()`) explicitly invoke `TemporalTaskStateRepository.delete(task_id=task_id)`.
- All terminal outcomes guarantee that no orphan `temporal_task_states` rows remain attached to completed, failed, or cancelled tasks.

---

## Section 3: Prioritized Reduction Candidates

The 31 fields in `OrchestratorState` are defined across workflow steps. Under Wave 1, `progress_updates` is excluded from `TemporalTaskState` snapshots at the persistence boundary, while the remaining 30 fields are serialized across activity boundaries. Further reduction must follow a strictly prerequisite-gated wave sequence:

```mermaid
flowchart TD
    subgraph Wave 1: Immediate Safe Exclusions [Wave 1: Pure Ephemeral]
        W1A["progress_updates<br/>(legacy accumulation; independent notifier)"]
    end

    subgraph Wave 2: Prerequisite-Gated Reductions [Wave 2: Consumer / Idempotency Gated]
        W2A["friction_reports<br/>(gated by Temporal projection policy)"]
        W2B["memory_to_persist<br/>(gated by persist_memory activity refactor)"]
        W2C["timeline_events<br/>(gated by _has_event idempotency refactor)"]
        W2D["scout_phase_results<br/>(relational in proposals/artifacts)"]
        W2E["session_state_update<br/>(relational in session_states)"]
        W2F["errors<br/>(relational in tasks.last_error)"]
    end

    subgraph Wave 3: Plan & Outcomes [Wave 3: Relational Rehydrators]
        W3A["task_plan / decomposed_plan<br/>(relational in execution_plans/nodes)"]
        W3B["node_outcomes<br/>(relational in execution_plan_node_attempts)"]
    end

    Wave 1 --> Wave 2 --> Wave 3
```

### Wave 1: Immediate Safe Exclusions (Completed)

#### 1. `progress_updates`
- **Current Behavior:** Transient notification strings accumulated in state during activity execution; excluded from `TemporalTaskState` snapshots at the persistence boundary.
- **Why Safe to Exclude:** `progress_updates` has no semantic or control-flow consumers in Temporal execution. Some legacy nodes read prior values only to preserve accumulated progress history; product progress notifications are independently constructed and emitted by `_notify_progress()` from explicit `phase`/`summary` arguments.
- **Status:** **Completed (M28.5B Wave 1)**. `EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS` omits `progress_updates` from all `TemporalTaskState` writes, while runtime `OrchestratorState.progress_updates` defaults to `[]` upon snapshot reload.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **What Breaks if Wrong:** Nothing; no control-flow, persistence, notification, or behavioral dependency exists on previous progress messages.
- **Rollback Path:** Additive re-inclusion.

---

### Wave 2: Prerequisite-Gated State Reductions (Consumer / Idempotency Gated)

#### 2. `friction_reports`
- **Current Behavior:** Friction reports extracted by worker/evaluator or generated during verification are kept in state and serialized.
- **Why Gated:** In the Temporal completion path, `_persist_state()` explicitly sets `persist_friction_proposals=False`, meaning friction reports are not currently projected to the `proposals` table (where improvement proposals are stored with `ProposalType.REFLECTION` and `reflection_kind="improvement_suggestion"`). Verification can also generate new friction reports mid-flight.
- **Prerequisite for Exclusion:** Explicitly establish whether friction reports in Temporal tasks should be durably persisted as reflection proposals or intentionally discarded, and align the persistence path before pruning from the state blob.
- **Risk Level:** **Low to Medium**
- **Size Impact:** Low to Medium
- **Rollback Path:** Additive re-inclusion.

#### 3. `memory_to_persist`
- **Current Behavior:** Memory candidates identified during task execution are serialized into the blob.
- **Downstream Consumer:** The Temporal `persist_memory` activity explicitly reloads state and executes `self.persist_memory_node` -> `_admit_memory_candidates`, which iterates over `state.memory_to_persist`.
- **Prerequisite for Exclusion:** Prove and test that `ObservationMemoryBridge` fully supersedes `persist_memory_node`, or decouple `persist_memory_node` from `state.memory_to_persist` so that memory candidates are passed directly or loaded from observation records.
- **Risk Level:** **Medium** (blocked by active consumer)
- **Size Impact:** Medium
- **Rollback Path:** Additive re-inclusion.

#### 4. `timeline_events`
- **Current Behavior:** Every timeline event is appended to `state.timeline_events` and serialized into the JSON blob. Concurrently, `_persist_intermediate_state` writes new events to `task_timeline_events` in the relational database.
- **Downstream Consumer:** `TaskExecutionActivities._has_event()` explicitly scans `state.timeline_events` as an **idempotency and retry guard** across 7 activities:
  - `classify_and_plan` (`TimelineEventType.TASK_SPEC_AND_ROUTE_GENERATED`)
  - `load_memory` (`TimelineEventType.MEMORY_LOADED`)
  - `provision_workspace` (`TimelineEventType.WORKSPACE_PROVISIONED` / `ENVIRONMENT_INITIALIZED`)
  - `run_worker` (`TimelineEventType.WORKER_COMPLETED` / `WORKER_FAILED` / `WORKER_ERROR`)
  - `verify_result` (`TimelineEventType.VERIFICATION_COMPLETED` / `VERIFICATION_SKIPPED`)
  - `deliver_result` (`TimelineEventType.TASK_COMPLETED` / `TASK_FAILED`)
  - `persist_memory` (`TimelineEventType.MEMORY_PERSISTED`)
- **Why Pruning Today Breaks Execution:** `_get_current_state()` reconciles the persisted event **count**, but does not reload historical events into `state.timeline_events`. If `timeline_events` is omitted from the snapshot, `_has_event()` evaluates to `False` on activity retry or resumption, causing side-effecting activities (e.g. workspace provisioning, delivery) to re-execute unexpectedly.
- **Prerequisite for Exclusion:** Refactor `_has_event()` to query `task_timeline_events` in Postgres, reconstruct event markers into `state.timeline_events` on reload, or introduce a compact durable idempotency marker (e.g. `executed_activity_keys: set[str]`), backed by retry/resume integration tests.
- **Risk Level:** **Medium** (high risk if pruned without prerequisite)
- **Size Impact:** **Very High** (reduces state blob size by 50–80% on multi-step and repair runs).
- **Rollback Path:** Additive re-inclusion.

#### 5. `scout_phase_results`
- **Current Behavior:** Captures intermediate scout summaries across repo/research phases.
- **Why Prerequisite Needed:** Stored in `proposals.metadata_payload["scout_phase_metadata"]` and artifact records; must verify that final scout aggregation queries proposals/artifacts directly rather than relying on state memory.
- **Risk Level:** **Low to Medium**
- **Size Impact:** Medium to High
- **Rollback Path:** Additive re-inclusion.

#### 6. `session_state_update`
- **Current Behavior:** Compact session state update dict stored in state.
- **Why Prerequisite Needed:** Authoritative store is `session_states` table in Postgres. Must verify that resumed worker context queries `SessionStateRepository` directly.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **Rollback Path:** Additive re-inclusion.

#### 7. `errors`
- **Current Behavior:** Cumulative list of string errors across workflow attempts.
- **Why Prerequisite Needed:** Authoritative store is `tasks.last_error` and `task_timeline_events` (`TASK_FAILED`). Verify no legacy UI/API paths depend on state blob errors.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **Rollback Path:** Additive re-inclusion.

---

### Wave 3: Plan & Node Outcomes (Medium-High Complexity)

#### 8. `task_plan`, `decomposed_plan`, `node_outcomes`
- **Current Behavior:** The entire DAG plan, node dependencies, task specs, and execution outcomes/attempts are serialized in the JSON blob.
- **Relational Authority:** `execution_plans`, `execution_plan_nodes`, and `execution_plan_node_attempts`.
- **Why Deferred to Wave 3:** The Temporal DAG scheduler reads directly from `state.decomposed_plan` throughout the activity lifecycle:
  - `select_next_node`: builds node maps from `state.decomposed_plan.nodes`.
  - `run_decomposed_node`: retrieves executable node contracts from `state.decomposed_plan.nodes`.
  - `merge_node_wave`: accesses `state.decomposed_plan.nodes` and `state.node_outcomes` to calculate wave completions and continuations.
- **Prerequisite for Removal:** Implement full reconstruction / read-through for `task_plan` and `decomposed_plan` (e.g. `ExecutionPlanRepository.load_decomposed_plan()` and `load_node_outcomes()`), or refactor the DAG scheduler to operate directly on `execution_plans` and `execution_plan_nodes` relational rows before removing them from the state blob.
- **Risk Level:** **Medium to High**
- **Size Impact:** High on large DAG workflows.
- **Rollback Path:** Additive re-inclusion.

---

## Section 4: Prerequisites per Reduction Slice

To guarantee zero regressions when reducing `TemporalTaskState` serialization in future PRs, each reduction slice must satisfy these five gates:

1. **Temporal Replay Test:**
   - Execute complete Temporal workflow replay tests using recorded workflow histories (e.g. `tests/integration/test_temporal_runtime.py`).
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
| **Proposals & Friction** | `friction_reports` | `temporal_task_states.state` | `proposals` (when enabled) / Ephemeral | **Wave 2 (Gated)** | Establish projection policy, then exclude from blob |
| **Timeline & Events** | `timeline_events`, `timeline_persisted_count` | `temporal_task_states.state` | `task_timeline_events` | **Wave 2 (Gated)** | Refactor `_has_event` to DB/idempotency marker, then exclude `timeline_events` |
| **Memory & Ephemeral Updates** | `memory_to_persist`, `scout_phase_results`, `session_state_update`, `errors` | `temporal_task_states.state` | `memory_*`, `proposals`, `session_states`, `tasks` | **Wave 2 (Gated)** | Verify/retire legacy activity consumers, then exclude from blob |
| **DAG Plan & Nodes** | `task_plan`, `decomposed_plan`, `node_outcomes` | `temporal_task_states.state` | `execution_plans`, `execution_plan_nodes` | **Wave 3 (Planned)** | Reconstruct via `ExecutionPlanRepository` |

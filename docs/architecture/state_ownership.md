# State Ownership Contract

## Purpose

This document is the authoritative field-level state ownership contract for
`code-agent` under Phase 4A (Milestone M28.5B). It establishes:

1. **Field-level authority and projection mapping** across Temporal history,
   `TemporalTaskState`, and relational Postgres tables.
2. **Lifecycle recovery rules** for activity replay, workflow restarts, and
   disaster recovery.
3. **A prioritized, risk-rated reduction plan** to eliminate redundant
   serialization in `TemporalTaskState` without breaking replay or query
   consumers.
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
     across activity boundaries.
   - At terminal workflow completion (`COMPLETED`, `FAILED`, `CANCELLED`),
     `TemporalTaskState` is **deleted**. Relational tables are already the 100%
     durable record of all completed work.

4. **New features must not deepen state duplication:**
   - New orchestration fields must define their authoritative store and write
     paths before adding fields to `TemporalTaskState`.
   - Redundant fields must be eliminated incrementally with explicit replay
     and reconstruction tests.

---

## Section 1: Field-Level Authority Table

`OrchestratorState` defines 31 fields passed between workflow steps. The table
below maps each field to its authoritative store, projection targets, write paths,
and read consumers across all 7 state stores:

1. **Temporal Workflow History** (deterministic execution trace)
2. **`temporal_task_states`** (encrypted JSON blob checkpoint)
3. **`tasks`** table (top-level task status, constraints, route, spec)
4. **`worker_runs`** table (per-attempt worker execution evidence)
5. **`execution_plans` / `execution_plan_nodes` / `execution_plan_node_attempts`** (DAG structure and node outcomes)
6. **`task_timeline_events`** (append-only timeline log)
7. **`human_interactions` / `session_states` / `proposals` / `memory_*`** (domain-specific relational entities)

| Field | Authoritative Store | Projection Stores | Write Path(s) | Read Consumers |
|---|---|---|---|---|
| `current_step` | Temporal Workflow History | `temporal_task_states.state`, `tasks.status`, `task_timeline_events` | `orchestrator/temporal/activities.py` (`_persist_intermediate_state`) | Workflow step dispatch, Dashboard status/timeline |
| `session` | `tasks` & `sessions` tables (`session_id`, `user_id`, `channel`) | `temporal_task_states.state` | Ingress handlers (`apps/api/routes/tasks.py`, `webhooks.py`, `telegram.py`) | Session continuity, `/sessions/{id}`, `/tasks/{id}`, Dashboard |
| `task` | `tasks` table (`id`, `repo_url`, `branch`, `task_text`, `constraints`, `budget_*`) | `temporal_task_states.state` | Ingress handlers, permission escalation in `orchestrator/temporal/activities.py` | Worker dispatch activities, API `/tasks/{id}`, Dashboard |
| `normalized_task_text` | Ingest/Planner domain stage | `temporal_task_states.state` | `orchestrator/nodes/ingest_node.py` | Prompt generation, worker routing policy |
| `task_kind` | `tasks.task_spec["task_kind"]` / `tasks.constraints["task_type"]` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Workflow branching, capability generation |
| `task_plan` | `execution_plans` & `execution_plan_nodes` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | DAG execution loop, Dashboard DAG view, `/tasks/{id}/plan` |
| `task_spec` | `tasks.task_spec` (JSONB) | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Capability grants, API `/tasks/{id}`, Dashboard |
| `decomposed_plan` | `execution_plans` & `execution_plan_nodes` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py`, `ExecutionPlanRepository` | Temporal DAG activity scheduler (`run_plan_node`, `merge_node_wave`), Dashboard |
| `node_outcomes` | `execution_plan_nodes` & `execution_plan_node_attempts` | `temporal_task_states.state` | `orchestrator/temporal/activities.py` (`merge_node_wave`), `ExecutionPlanRepository` | Wave merge loop, retry decisions, Dashboard DAG node details |
| `current_node_id` | Temporal Workflow History | `temporal_task_states.state` | `orchestrator/temporal/activities.py` (`run_plan_node`) | Node execution tracing & logs |
| `repo_profile` | Ephemeral / Workspace Discovery | `temporal_task_states.state` | Discovery / planning activities in `orchestrator/` | Worker prompt context |
| `memory` | `memory_items`, `memory_observations`, `session_states` | `temporal_task_states.state` | Loaded on ingest (`build_orchestrator_graph_input`) | Worker dispatch prompt assembly |
| `route` | `tasks` (`chosen_worker`, `chosen_profile`, `runtime_mode`, `route_reason`) & `worker_runs` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_update_task_route_and_spec`) | Worker dispatch, `/tasks/{id}`, Dashboard |
| `approval` | `human_interactions` (`type="approval"`) & `tasks.constraints["approval"]` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_apply_approval_constraints`), `HumanInteractionRepository` | API `/tasks/{id}/approve`, `/tasks/{id}/reject`, Dashboard HITL cards |
| `dispatch` | `worker_runs` (`worker_type`, `worker_profile`, `runtime_mode`, `runtime_manifest`) | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Sandbox runtime executor, Dashboard run details |
| `result` | `worker_runs` & `artifacts` (patches, diffs, logs, outputs) | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_create_worker_run`, `_persist_artifacts_for_run`) | Completion loop (repair/delivery), API `/tasks/{id}/runs`, Dashboard |
| `verification` | `worker_runs.verifier_outcome` (JSONB) & `artifacts` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_create_worker_run`) | Completion loop repair decisions, Dashboard Verification tab |
| `review` | `artifacts` (`type="independent_review_result"`) & `worker_runs.artifact_index` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_persist_artifacts_for_run`) | Completion loop repair decisions, Dashboard Review tab |
| `friction_reports` | `proposals` table (`proposal_type="friction"`) | `temporal_task_states.state` | `orchestrator/execution_improvement_proposal_service.py` | API `/proposals`, Dashboard Proposals tab (never read back from state blob) |
| `memory_to_persist` | `memory_items` & `memory_observations` | `temporal_task_states.state` | Post-commit `ObservationMemoryBridge.bridge_observations` | Memory repositories (never read back from state blob) |
| `progress_updates` | Ephemeral / Notification webhooks & `task_timeline_events` | `temporal_task_states.state` | `orchestrator/temporal/activities.py` notifier | Real-time notification receivers (never read back from state blob) |
| `timeline_events` | `task_timeline_events` table | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_persist_timeline_events`) | Dashboard Timeline, API `/tasks/{id}/timeline` |
| `timeline_persisted_count` | Derived cursor (`TaskTimelineRepository.count_by_attempt`) | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_persist_intermediate_state`), reconciled in `_get_current_state` | Timeline batch insertion deduplication cursor |
| `repair_handoff_requested` | `tasks.constraints["repair_handoff_requested"]` & Temporal workflow state | `temporal_task_states.state` | `orchestrator/temporal/activities.py` | Delivery activity, manual handoff notifications |
| `completion_loop` | `tasks.constraints` (`*_repair_passes_used`) & Temporal Workflow History | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_apply_completion_control_constraints`) | Workflow completion loop routing, repair budget enforcement |
| `errors` | `tasks.last_error` & `task_timeline_events` (`TASK_FAILED`) | `temporal_task_states.state` | `orchestrator/temporal/activities.py` (`record_workflow_failure`) | API `/tasks/{id}`, Dashboard error banners |
| `attempt_count` | `tasks.attempt_count` column | `temporal_task_states.state` | `TaskRepository.increment_attempt_count` | Timeline attempt partitioning, retry limits, Dashboard attempt history |
| `session_state_update` | `session_states` table (`active_goal`, `decisions_made`, `identified_risks`, `files_touched`) | `temporal_task_states.state` | `orchestrator/temporal/activities.py` (`_persist_rejected_session_state`, outcome persistence) | Subsequent task memory context, API `/sessions/{id}`, Dashboard |
| `scout_phase` | Temporal Workflow History / `tasks.constraints["scout_phase"]` | `temporal_task_states.state` | Scout activities | Scout prompt selection, proposal metadata builder |
| `scout_phase_results` | `proposals` (in `metadata_payload`) & `artifacts` | `temporal_task_states.state` | `orchestrator/execution_outcome_service.py` (`_merge_scout_phase_result`) | Final scout proposal synthesis |
| `fanout_disabled_for_remainder` | Temporal Workflow History | `temporal_task_states.state` | `orchestrator/temporal/activities.py` (`merge_node_wave`) | Subsequent wave scheduling (forces serial execution) |

---

## Section 2: Recovery Rules

### 1. Temporal Activity Replay
- When a worker restarts or an activity fails, Temporal replays the workflow event history up to the point of failure.
- Completed activities are **not re-executed**; their recorded outputs are replayed deterministically from Temporal history.
- When an in-flight activity begins or retries, it calls `_get_current_state(task_id)`:
  - If a snapshot exists in `TemporalTaskState`, it parses the state and reconciles approval status (`task.constraints["approval"]`) and timeline event count (`TaskTimelineRepository.count_by_attempt`).
  - If no snapshot exists (e.g. initial workflow start), it reconstructs the initial state from `tasks` and `task_submissions` via `_load_submission_for_task` and `build_orchestrator_graph_input`.

### 2. Workflow Restart / Continue-As-New
- When a workflow is restarted or continued-as-new, durable progress must be reconstructable without relying on deleted in-memory state.
- Relational tables (`tasks`, `worker_runs`, `execution_plans`, `human_interactions`, `session_states`) contain all historical attempts and decisions.
- Excluding fields from `TemporalTaskState` is safe if and only if:
  - The field is ephemeral to a single activity invocation (e.g. `progress_updates`, `friction_reports`, `memory_to_persist`), OR
  - The field is reconstructable from relational tables (e.g. `timeline_events` loaded from `task_timeline_events`).

### 3. Postgres-Only Recovery (Disaster Recovery / Temporal Reset)
- If Temporal history is completely lost, all completed tasks are already 100% intact because `TemporalTaskState` is deleted upon terminal completion.
- In-flight tasks can be reconstructed up to the last committed activity boundary from `tasks`, `worker_runs`, `execution_plan_nodes`, and `task_timeline_events`.

### 4. Terminal State Cleanup
- Terminal activities (`_persist_state`, `record_workflow_failure`, permission rejection) explicitly invoke `TemporalTaskStateRepository.delete(task_id=task_id)`.
- No lingering JSON blobs are retained in `temporal_task_states` for completed, failed, or cancelled tasks.

---

## Section 3: Prioritized Reduction Candidates

The 31 fields in `OrchestratorState` are currently serialized into `TemporalTaskState` at every activity boundary. The following candidates have been evaluated for safe exclusion or pruning from `TemporalTaskState`:

```mermaid
flowchart TD
    subgraph Wave 1: Immediate Safe Exclusions [Wave 1: Pure Redundancy]
        W1A["timeline_events<br/>(relational in task_timeline_events)"]
        W1B["progress_updates<br/>(ephemeral notifications)"]
        W1C["friction_reports<br/>(relational in proposals)"]
        W1D["memory_to_persist<br/>(relational in memory_items)"]
    end

    subgraph Wave 2: Semi-Static Context [Wave 2: Reconstructable from DB]
        W2A["scout_phase_results<br/>(relational in proposals/artifacts)"]
        W2B["session_state_update<br/>(relational in session_states)"]
        W2C["errors<br/>(relational in tasks.last_error)"]
    end

    subgraph Wave 3: Plan & Outcomes [Wave 3: Plan Relational Alignment]
        W3A["task_plan / decomposed_plan<br/>(relational in execution_plans/nodes)"]
        W3B["node_outcomes<br/>(relational in execution_plan_node_attempts)"]
    end

    Wave 1 --> Wave 2 --> Wave 3
```

### Wave 1: Immediate Safe Exclusions (Low Risk, High Size Impact)

#### 1. `timeline_events`
- **Current Behavior:** Every timeline event is appended to `state.timeline_events` and serialized into the JSON blob. Concurrently, `_persist_intermediate_state` writes new events to `task_timeline_events` in the relational database.
- **Why Safe to Exclude:** The relational table `task_timeline_events` is written in the exact same transaction before snapshotting. On state reload, `_get_current_state` already counts persisted events via `TaskTimelineRepository.count_by_attempt()`.
- **Risk Level:** **Low**
- **Size Impact:** **Very High** (reduces state blob size by 50–80% on multi-step and repair runs).
- **What Breaks if Wrong:** If an activity expects `state.timeline_events` to contain the full history rather than relying on `task_timeline_events`, it would see an empty list. (Audit confirms activities only append new events and read `timeline_persisted_count`).
- **Rollback Path:** Re-add `timeline_events` to serialization.

#### 2. `progress_updates`
- **Current Behavior:** Transient notification strings collected during execution and serialized into the blob.
- **Why Safe to Exclude:** Ephemeral; passed directly to external webhook / Telegram notifiers during activity execution. Never read back from the snapshot by any downstream activity.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **What Breaks if Wrong:** Nothing; no read consumers exist in subsequent activities.
- **Rollback Path:** Additive re-inclusion.

#### 3. `friction_reports`
- **Current Behavior:** Friction reports extracted by worker/evaluator are kept in state and serialized.
- **Why Safe to Exclude:** Projected to the `proposals` table (`proposal_type="friction"`) in `_persist_friction_proposals_if_needed`. Downstream activities and workflows never read `friction_reports` from `TemporalTaskState`.
- **Risk Level:** **Low**
- **Size Impact:** Low to Medium
- **What Breaks if Wrong:** Nothing; proposals are queried from the `proposals` table by the API and dashboard.
- **Rollback Path:** Additive re-inclusion.

#### 4. `memory_to_persist`
- **Current Behavior:** Memory candidates identified during task execution are serialized into the blob.
- **Why Safe to Exclude:** Consumed immediately post-commit by `ObservationMemoryBridge.bridge_observations` and written to `memory_items` and `memory_observations`. Never read back from the snapshot.
- **Risk Level:** **Low**
- **Size Impact:** Medium
- **What Breaks if Wrong:** Nothing; memory query systems query `memory_items` directly.
- **Rollback Path:** Additive re-inclusion.

---

### Wave 2: Semi-Static & Derived Context (Low-to-Medium Risk)

#### 5. `scout_phase_results`
- **Current Behavior:** Captures intermediate scout summaries across repo/research phases.
- **Why Safe to Exclude:** Already stored in `proposals.metadata_payload["scout_phase_metadata"]` and as artifact records.
- **Risk Level:** **Low**
- **Size Impact:** Medium to High
- **What Breaks if Wrong:** Final scout aggregation activity if it does not query artifacts/proposals.
- **Rollback Path:** Additive re-inclusion.

#### 6. `session_state_update`
- **Current Behavior:** Compact session state update dict (`active_goal`, `decisions_made`, `identified_risks`, `files_touched`) stored in state.
- **Why Safe to Exclude:** Authoritative store is `session_states` table in Postgres, updated via `SessionStateRepository.upsert`.
- **Risk Level:** **Low**
- **Size Impact:** Low
- **What Breaks if Wrong:** Resumed worker context if it reads from state object instead of `SessionStateRepository`.
- **Rollback Path:** Additive re-inclusion.

#### 7. `errors`
- **Current Behavior:** Cumulative list of string errors across workflow attempts.
- **Why Safe to Exclude:** Authoritative store is `tasks.last_error` and `task_timeline_events` (`TASK_FAILED`).
- **Risk Level:** **Low**
- **Size Impact:** Low
- **What Breaks if Wrong:** Error display in legacy code paths if any read `state.errors`.
- **Rollback Path:** Additive re-inclusion.

---

### Wave 3: Plan & Node Outcomes (Medium Risk, High Complexity)

#### 8. `task_plan`, `decomposed_plan`, `node_outcomes`
- **Current Behavior:** The entire DAG plan, node dependencies, task specs, and execution outcomes/attempts are serialized in the JSON blob.
- **Relational Authority:** `execution_plans`, `execution_plan_nodes`, and `execution_plan_node_attempts`.
- **Why Deferred to Wave 3:** Temporal DAG activities (`run_plan_node`, `merge_node_wave`) currently access `state.node_outcomes` in memory to compute wave continuations and aggregate results.
- **Prerequisite for Removal:** Implement a dedicated `ExecutionPlanRepository.load_node_outcomes(task_id)` reconstructor that populates `state.node_outcomes` directly from the relational tables before removing it from the state blob.
- **Risk Level:** **Medium**
- **Size Impact:** High on large DAG workflows.
- **Rollback Path:** Additive re-inclusion.

---

## Section 4: Prerequisites per Reduction Slice

To guarantee zero regressions when reducing `TemporalTaskState` serialization in future PRs, each reduction slice must satisfy these four gates:

1. **Temporal Replay Test:**
   - Execute a complete Temporal workflow replay test using recorded workflow histories (e.g. `tests/integration/test_temporal_runtime.py`).
   - Verify that the workflow history replays deterministically without non-determinism errors.

2. **Projection Verification:**
   - Assert that the authoritative relational table (e.g. `task_timeline_events`, `proposals`, `session_states`) receives complete, well-formed rows during normal execution.
   - Assert that the corresponding API endpoints and dashboard queries continue returning expected data.

3. **Reconstruction Test:**
   - Add unit/integration tests verifying that `_get_current_state` properly reconstructs any required state fields from relational tables if an activity is restarted mid-workflow.

4. **Additive Rollback Guarantee:**
   - Serialization pruning changes must be backward-compatible: deserialization logic should use default values (`Field(default_factory=...)`) when reading older snapshots that still contain or omit the pruned fields.
   - Re-adding any pruned field to `model_dump()` remains a safe, instant rollback.

---

## Summary Matrix

| State Category | Fields | Authoritative Store | Elimination Priority | Target State |
|---|---|---|---|---|
| **Coordination & Step** | `current_step`, `current_node_id`, `completion_loop`, `fanout_disabled_for_remainder`, `repair_handoff_requested` | Temporal History | Retain in Checkpoint | Compact activity control struct |
| **Ingress & Spec** | `session`, `task`, `task_spec`, `task_kind`, `normalized_task_text`, `repo_profile`, `memory`, `route`, `approval`, `attempt_count` | `tasks`, `sessions`, `human_interactions` | Retain in Checkpoint (reference IDs where feasible) | Read-through from `tasks` table |
| **Execution Outcomes** | `dispatch`, `result`, `verification`, `review` | `worker_runs`, `artifacts` | Retain in Checkpoint during active turn | Deleted on terminal completion |
| **Timeline & Events** | `timeline_events`, `timeline_persisted_count`, `progress_updates` | `task_timeline_events` | **Wave 1 (Immediate)** | **Exclude `timeline_events` from blob**; use cursor count only |
| **Proposals & Memory** | `friction_reports`, `memory_to_persist`, `scout_phase_results`, `session_state_update` | `proposals`, `memory_*`, `session_states` | **Wave 1 & 2** | **Exclude from blob**; write direct to relational tables |
| **DAG Plan & Nodes** | `task_plan`, `decomposed_plan`, `node_outcomes` | `execution_plans`, `execution_plan_nodes` | **Wave 3 (Planned)** | Reconstruct via `ExecutionPlanRepository` |

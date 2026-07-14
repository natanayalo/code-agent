# M24.9.5 — Temporal Runtime Consolidation

## Purpose

Consolidate the execution boundaries proven by the M24.9 Temporal PoC before
M25 adds concurrent DAG execution. This is a simplification milestone, not a
second orchestration implementation and not a broad rewrite.

The PoC decision is carried forward here as the implementation boundary for
M25. Supporting ownership and retirement details are in the
[LangGraph boundary audit](m24_9_5_langgraph_boundary_audit.md) and
[legacy drain plan](m24_9_5_legacy_drain_plan.md).

## Target boundary

| Owner | Responsibilities |
| --- | --- |
| Temporal | Durable lifecycle sequencing, activity dispatch, retry/timeout policy, signal waits, cancellation delivery, and future bounded fan-out/fan-in. |
| Postgres product projection | Tasks, interactions, timeline, artifacts, dashboard queries, and user-visible delivery state. |
| code-agent domain logic | Task classification, planning/decomposition, worker/provider behavior, workspace policy, validation, review, and memory governance. |
| Legacy runtime | Explicit fallback only while existing tasks and operational parity are drained and verified. |

## Current ownership audit

| Concern | Current implementation | Target after consolidation | Status |
| --- | --- | --- | --- |
| New-task runtime selection | `apps/runtime.py` resolves `CODE_AGENT_EXECUTION_RUNTIME`, with the legacy boolean retained only for compatibility. | Temporal is the normal sequential runtime; legacy execution is an explicit fallback. | Complete for the Compose path. |
| Legacy task claiming and leases | `repositories/sqlalchemy_task.py`, `orchestrator/execution_*_service.py`, and `apps/worker/main.py`. | Retain unchanged for fallback/drain; freeze feature work and identify deletion prerequisites. | Audit complete; no deletion yet. |
| Temporal execution | `orchestrator/temporal/workflows.py`, `activities.py`, `worker.py`, and `queues.py`. | Primary durable runtime for sequential work and M25 DAG scheduling. | Proven by M24.9. |
| Worker registry and load accounting | `WorkerNode` persistence plus queue claim/lease/heartbeat mechanics. | Keep product-facing worker profile/capability policy; do not treat Temporal queues as a one-for-one registry replacement. | Shrink plan required. |
| Human approval | API/dashboard persist an interaction; `TaskExecutionWorkflow.handle_approval` resumes the workflow. | DB review card plus idempotent Temporal signal/update; no new LangGraph interrupt dependency. | Proven; hardening inventory required. |
| Cancellation | API persists terminal cancellation and requests Temporal cancellation when selected. | Standardize workflow/activity cancellation projection and provider cleanup behavior. | Delivery is covered; provider cleanup contract remains. |
| Timeline projection | API/dashboard and Temporal activities can write timeline events. | Idempotent, monotonic, retry-safe projection keyed by stable event identity where needed. | Initial reconciliation fix exists; contract required. |
| Workflow checkpoints | `orchestrator/graph.py` still owns LangGraph graph/checkpoint behavior on the legacy path. | Temporal history owns Temporal-path lifecycle; Postgres remains the product projection. | Temporal path is independent; keep legacy support until interaction parity. |
| Agent reasoning | `OrchestratorBrain` protocol plus worker/domain functions. | Retain the existing brain and Worker seams; extract only when a concrete Temporal need exceeds them. | Boundary audit complete; interaction parity remains. |
| Memory lifecycle | Existing repository and governance logic, called from orchestration. | Invoke through idempotent Temporal activities; retain current memory backend/governance. | Deferred implementation. |

## Delivery slices

### M24.9.5a — Runtime ownership and default-routing guardrails — Complete

- Introduce an explicit execution-runtime policy rather than relying on an
  ambiguous optional PoC flag.
- Make Temporal the default in the production-like Compose path only when the
  Temporal service and Temporal worker are enabled.
- Keep legacy selection explicit for API-only development, test isolation,
  incident fallback, and drain.
- Add configuration and submission tests proving the selected runtime is
  observable and that a Temporal startup failure projects a terminal product
  failure rather than silently falling through to legacy execution.

**Exit criteria:** a new stack task uses Temporal by default; a caller can
explicitly select the legacy fallback; no automatic mixed execution occurs.

### M24.9.5b — Lifecycle and projection contract

**Complete:** retry, start-to-close timeout, heartbeat timeout, and
profile-derived worker-queue policy now live in
`orchestrator/temporal/policy.py` and are covered by unit and Temporal
integration tests. A waiting Temporal workflow now has direct integration
coverage proving that cancellation leaves one terminal product projection and
timeline event. A direct Temporal `run_worker` cancellation regression now
proves cancellation reaches the worker-await cleanup path. Multi-writer event
identity is persisted through stable timeline event keys. Final delivery now
treats a failed verification report as authoritative, preventing a worker
success from becoming a false completed task.

- Define retry, start-to-close timeout, heartbeat timeout, cancellation, and
  terminal-failure policy per Temporal activity type.
- Add idempotency keys/event identities for projections that can be retried or
  written by both an operator action and a workflow activity.
- Add focused cancellation and long-running worker heartbeat coverage for the
  primary provider path.

**Exit criteria:** retries, cancellation, and terminal failure always leave one
consistent product status and a monotonic timeline.

### M24.9.5c — HITL and LangGraph boundary audit

**Complete:** the [LangGraph boundary audit](m24_9_5_langgraph_boundary_audit.md)
classifies existing ownership and confirms that Temporal does not compile or
restore LangGraph state. Clarification now has a real Temporal pause/resume
test through the persisted dashboard interaction and `handle_clarification`
signal. Worker permission escalation now has deterministic grant, rejection,
duplicate-response, and cancellation coverage; no Temporal-path interaction
depends on a LangGraph checkpoint.

- Inventory every approval/resume and durable-control-flow use in
  `orchestrator/graph.py`.
- Route new approval waits through DB interaction state plus Temporal
  signal/update semantics.
- Define `BrainPort` and `NodeRunnerPort` boundaries; move only durable
  lifecycle concerns out of the graph.

**Exit criteria:** no new Temporal-path control flow depends on a LangGraph
checkpoint or interrupt; remaining LangGraph uses are classified as brain or
legacy-only behavior.

### M24.9.5d — Legacy drain and deletion candidates

**Complete:** the [legacy drain plan](m24_9_5_legacy_drain_plan.md) classifies
Temporal as primary, identifies queue/lease and LangGraph lifecycle code as
fallback-only deletion candidates, and preserves worker policy and Postgres
projections as custom code. It records completed permission-escalation parity
and measurable retirement gates.

- Mark legacy queue/lease/heartbeat code as fallback-only in operator docs and
  code comments where appropriate.
- List code eligible for removal only after the Temporal path has parity,
  cancellation coverage, and an agreed fallback retirement date.
- Keep worker profile/capability policy separate from mechanical dispatch.

**Exit criteria:** a concrete deletion list and measurable drain conditions
exist; no legacy execution mechanism is removed prematurely.

## Non-goals

- Parallel node execution, child workflows, aggregation, or fan-in.
- Mutable-node worktrees, patch merging, conflict resolution, or final merge
  policy.
- Multi-machine capacity scaling or worker autoscaling.
- Replacing the memory backend or deleting all LangGraph code.
- Making the dashboard query Temporal history directly.

## M25 entry criteria

M25 may begin only when:

1. Temporal is the documented default for new sequential tasks in the
   production-like local stack, with an explicit legacy fallback.
2. Retry, terminal failure, cancellation, and heartbeat policies are documented
   and covered for the main long-running provider path.
3. Product projection writes are idempotent and safe for operator/workflow
   multi-writer interaction.
4. The remaining legacy queue, lease, worker-registry, and LangGraph
   responsibilities are explicitly classified as fallback-only, custom domain
   logic, or deletion candidates.

## M25 handoff

Once these criteria are met, M25.0 implements **bounded, read-only,
dependency-ready fan-out in Temporal**. It must not introduce an in-process
parallel scheduler beside the Temporal workflow runtime.

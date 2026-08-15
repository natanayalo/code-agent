# Status

## Current phase

Phase 4A: Temporal stabilization and measured reliability.

Completed milestone: **M26 — Review-Comment Repair**.

M26 is complete: review-comment repair polling on open draft PRs, author filtering, budget capping, deduplicated reply planning, immediate per-reply DB checkpointing, and thread replies are fully implemented and verified.

Completed milestone: **M28 — Memory Effectiveness and Session Continuity**.

M28's required real-worker evidence is complete on the hardened native-agent
executor boundary. The scoped matrix is effective: all eight Codex/Antigravity
cold-and-assisted pairs passed useful-hit, irrelevant-rejection,
stale-reverification, and conflict-handling gates.

Completed milestone: **M28.5B Wave 1 — State Reduction & Universal Terminal Cleanup**.

M28.5B Wave 1 is complete: `progress_updates` is excluded from intermediate
`TemporalTaskState` serialization at the persistence boundary while runtime
compatibility is preserved, and terminal snapshot deletion is universal across
all exit paths including initial approval rejection.

Completed slice: **M28.5B Wave 2 State Reduction Closeout — Candidate Fields Pruning & Canonical Resolution**.

M28.5B Wave 2 state reduction is complete: all remaining candidate fields
(`friction_reports`, `errors`, `session_state_update`, `scout_phase_results`,
`memory_to_persist`, and `timeline_events`) are excluded from intermediate
`TemporalTaskState` serialization at the persistence boundary. Canonical memory
resolution is centralized at `persist_memory_node` from retained `WorkerResult`,
session state is regenerated at consumption, the current Temporal workflow does
not schedule deep-scout multi-phase chaining (executing single-phase runs where
`state.result` is authoritative for proposals and artifacts), verification friction
is ephemeral, and error reporting is projected directly to `tasks.last_error` and
`TASK_FAILED` timeline events.

Completed slice: **M28.5B Wave 3A — DAG Plan Reconstruction & Pruning (`task_plan` & `decomposed_plan`)**.

M28.5B Wave 3A is complete: `task_plan` and `decomposed_plan` are pruned from
`TemporalTaskState` serialization, with authoritative plan contracts restored
directly from `TASK_PLANNED` timeline event payloads. `TaskPlan` dependency semantics
(`depends_on=None` vs `[]`) and planner metadata are preserved, pre-decomposition
lifecycles maintain `decomposed_plan=None` cleanly, relational projection validation
verifies immutable scheduler contracts against Postgres `execution_plans` and
`execution_plan_nodes`, and all 5 direct snapshot readers route through `_rehydrate_dag_state()`.
`node_outcomes` remains unexcluded until Wave 3B.

## Current capabilities

- authenticated API, generic webhook, and Telegram task intake
- durable Postgres persistence for users, sessions, tasks, worker runs,
  interactions, timelines, artifacts, memory, and delivery metadata
- transactional Temporal command outbox with idempotent start, signal, and
  cancellation delivery
- Temporal-owned task lifecycle, retry, timeout, signal wait, cancellation,
  and activity recovery
- TaskSpec generation, capability-aware worker routing, and manual overrides
- Codex and Antigravity native-agent workers plus opt-in OpenRouter execution
- isolated Docker workspaces with command, diff, test, and artifact evidence
- skeptical personal/project memory, compact session state, admission review,
  observation evidence, and full-text retrieval
- clarification, approval, permission escalation, cancellation, and replay
  controls through API and dashboard
- sequential decomposed-task DAG execution and opt-in bounded two-node
  read-only fan-out
- deterministic and independent verification, independent review, and
  workspace/branch/draft-PR delivery
- durable, bounded verifier and independent-review repair loops on retained
  workspaces with permission escalation, re-verification, and manual handoff
- automated review-comment polling and repair loop on open draft PRs with
  author filtering, budget capping, thread replies, and dashboard visibility
- dashboard visibility for tasks, TaskSpec, DAG attempts, interactions,
  timelines, logs, artifacts, traces, memory, proposals, metrics, tools,
  dependency readiness, degraded reasons, and safe recovery guidance
- public dependency-aware `/ready` and authenticated outbox, worker freshness,
  stuck-wait, and terminal-reconciliation metrics
- an incremental M25.6 evidence collector that pins deployment identity,
  captures Postgres and Temporal proof, rejects duplicate cases, and emits a
  sanitized aggregate without changing routing automatically

Completed work remains in [`CHANGELOG.md`](../CHANGELOG.md). The historical
Temporal migration and rollback record is in the
[cutover archive](archive/temporal_cutover.md).

## Current evidence

- The accepted M25.3 release covered the Temporal lifecycle, HITL, cancellation,
  restart recovery, sequential DAGs, opt-in fan-out, outage recovery, history
  replay, and terminal reconciliation.
- The accepted M25.4 release added patch-aware Temporal completion-loop parity:
  verifier and independent-review repair, retained-workspace execution, repair
  restart/idempotency/cancellation handling, replay coverage, and one terminal
  manual-follow-up projection when repair cannot complete.
- The accepted M25.5 slice proves structured dependency failure and same-process
  recovery, worker-owned dispatcher heartbeats, stale outbox gating,
  task-specific degraded signals, dashboard recovery guidance, responsive
  operator rendering, and a current-branch real-worker Temporal lifecycle.
- The reviewed M25.6 real-worker baseline contains 20/20 valid captures across
  monolithic read-only work, mutations, verifier and independent-review repair,
  sequential DAGs, read-only fan-out, HITL, cancellation, worker restart, and
  isolated Codex/Antigravity draft-PR delivery. Operator redaction review and
  the forbidden-field validator passed for the public reports. The result does
  not change routing automatically.
- The completed M26 slice adds automated review-comment polling on open draft
  PRs, structured prompt building for review comments, deduplicated reply
  planning, immediate per-reply DB checkpointing, and GitHub GraphQL thread replies.
- The M28.2 slice records typed task, route, approval, worker, verifier, and
  review outcomes in compact session state, then makes that bounded advisory
  context available to replayed and resumed workers.
- The reviewed M28 real-worker matrix contains 8/8 valid Codex/Antigravity
  pairs on the exact evaluated revision. It proves accepted project memory
  reaches native prompts and is used where expected; irrelevant, stale, and
  conflicting memory is rejected or suppressed as required. Questions and
  interventions did not regress (all captures were zero); mixed completion
  times are observations, not a timing-improvement claim.
- The 25-case frozen evaluation remains a deterministic domain-logic regression
  suite. It uses replayed worker outcomes and is not a real-provider Temporal
  reliability baseline.
- Performance routing currently consumes checked-in advisory metrics; it is not
  refreshed from persisted live task outcomes.
- Full-text memory retrieval passes the curated non-semantic regression cases
  and retains known synonym gaps. Current evidence does not justify adding a
  vector dependency.

## Known limitations

- bounded fan-out remains an explicit read-only pilot and is disabled by
  default
- deep-scout repo-to-research chaining is deferred and is not part of the
  supported Temporal completion loop
- M28's scoped matrix does not establish a general completion-time reduction;
  it is evidence of correct, safe memory behavior across the defined cases
- the trusted Temporal worker retains Docker authority, while native provider
  CLI execution is confined to one-shot task containers; a separate sandbox
  broker remains deferred
- lifecycle data currently overlaps across Temporal history/workflow state,
  serialized `TemporalTaskState`, task/product projections, execution-plan
  rows, and timeline events; Wave 1, Wave 2, and Wave 3A have pruned
  ephemeral `progress_updates`, candidate fields (`friction_reports`, `errors`,
  `session_state_update`, `scout_phase_results`, `memory_to_persist`, `timeline_events`),
  and DAG plan models (`task_plan`, `decomposed_plan`) from snapshots; Wave 3B.1
  establishes relational merge markers and attempt-aware rehydration with dual-write
  preservation of `node_outcomes` in snapshots (Wave 3B.2 will prune snapshot serialization)
- the worker boundary is currently terminal `WorkerRequest -> WorkerResult`;
  provider-neutral streaming `AgentEvent` and `ContextEnvelope` contracts are
  planned rather than available today
- native-agent command audit and several orchestration/worker adapters remain
  complexity hotspots
- the repository enforces a 90% Python coverage target in CI

## Next slices only

1. Continue M28.5 execution-architecture foundation work (M28.5A sandbox broker/threat model,
   M28.5C `AgentEvent`, M28.5D `ContextEnvelope`).
2. Use the M28 report as a scoped safety/effectiveness signal only; do not
   change routing or add semantic retrieval without further evidence.

## Deferred

- M27 reliability-based autonomy remains reserved until real-task metrics can
  support reversible policy thresholds, and additionally requires M29's
  expanded evidence.
- durable child workflows, broad mutable fan-out, isolated worktree/patch
  reconciliation, an operator-visible agent tree, and evidence-backed
  procedural skills remain future/conditional ideas rather than current work.

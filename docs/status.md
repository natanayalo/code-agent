# Status

## Current phase

Phase 4A: Temporal stabilization, execution architecture foundation, and measured reliability.

Active focus: **M28.5 — Execution Architecture Foundation** (M28.5A Sandbox/Trust Boundary, M28.5C AgentEvent, M28.5D ContextEnvelope).

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
- lifecycle data overlaps across Temporal history/workflow state, serialized
  `TemporalTaskState`, task/product projections, and execution-plan rows;
  intermediate state pruning (Waves 1-3) has eliminated ephemeral notifications,
  candidate metadata, plan models, and node outcomes from snapshots while
  relational projections and markers retain authoritative execution truth
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

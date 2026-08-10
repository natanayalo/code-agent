# Status

## Current phase

Phase 4A: Temporal stabilization and measured reliability.

Completed milestone: **M26 — Review-Comment Repair**.

M26 is complete: review-comment repair polling on open draft PRs, author filtering, budget capping, deduplicated reply planning, immediate per-reply DB checkpointing, and thread replies are fully implemented and verified.

Active focus: **M28 — Memory Effectiveness and Session Continuity**.


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
- compact session state records the active goal and touched files but does not
  yet extract structured decisions and risks from worker results
- native-agent command audit and several orchestration/worker adapters remain
  complexity hotspots
- the repository enforces a 90% Python coverage target in CI

## Next slices only

1. M28: Memory effectiveness and session continuity
   - prove durable memory and compact session state reduce repeated work without stale context control

## Deferred

- M27 reliability-based autonomy remains reserved until real-task metrics can
  support reversible policy thresholds, and additionally requires M29's
  expanded evidence.

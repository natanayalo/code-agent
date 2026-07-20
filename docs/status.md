# Status

## Current Phase

Phase 4: selective autonomy after reliability.

Active focus:

- M25.3 Temporal-only cutover and legacy retirement.
  - Slice 2 production cutover is complete: Temporal is the default runtime,
    submission failures degrade with API 503, workers fail fast, and dashboard
    drain metrics expose the persisted cutover boundary. The
    [observation evidence ledger](m25_3_observation_ledger.md) records the
    Compose-scenario gate, 7-day active soak, and ≥14-day/≥25-task retirement
    gate; legacy deletion and schema cleanup remain deferred to later slices.

## Phase 3 Reliability Baseline
- **Baseline cases**: 25 baseline cases run, 25 passed according to the frozen evaluation report.
- **Approval requests**: 1 case needing approval.
- **Validation evidence**: 24 cases with validation evidence present.
- **Manual log inspection**: 10 cases needing manual log inspection.
- **Worker failures**: 9 cases with worker failure (expected failure cases).

## Current Capabilities
- API + Telegram ingress for task intake
- shared-secret API auth for protected ingress routes
- durable Postgres persistence for users/sessions/tasks/runs/artifacts/memory
- split API/worker runtime with queue polling and lease claims
- LangGraph orchestrator with worker routing, approval checkpoints, verifier stage, and timeline persistence
- worker adapters for Codex CLI, Antigravity CLI, and OpenRouter-backed execution
- sandboxed workspace/container execution with command artifact capture and retention controls
- skeptical memory + compact session state persistence
- orchestrator loads skeptical personal/project/session memory before worker dispatch and persists typed worker-produced memory after runs
- operational controls: task replay, approval decision endpoint, progress callbacks, and metrics
- generated TaskSpec contract for task goal/risk/type/delivery policy before worker routing
- repo registry and validation profiles gate public repo selection, protected paths, and validation defaults
- deterministic advisory repository memory profiles are injected into worker context without changing repository policy
- M23.11 evaluation confirms the worker uses advisory profiles correctly, avoids stale policy, and improves task success without increasing unsafe actions
- M24.1–M24.6 provide validated sequential task decomposition, durable node contracts/evidence, crash-resilient per-node attempt history, sequential execution, parent-result aggregation, bounded retry, blocked-node resume, and a deterministic reliability gate
- M24.9 Temporal PoC is complete: feature-flagged Temporal lifecycle, profile queues, durable activity handoff, dashboard HITL signals, trace continuity, worker recovery, and legacy fallback have closing evidence
- M25.0 preserves explicit DAG dependencies, persists node-level fan-out metadata,
  validates parallel-safety contracts fail-closed, and retains legacy sequential behavior
- M25.1A durable node activity persistence is complete: deterministic logical activity
  identities, atomic claims/replay, fenced terminal results, legacy compatibility, and
  claim-loss cancellation now support the legacy sequential executor
- M25.1B Temporal one-node-wave orchestration is complete: version-gated workflow
  compatibility, compact select/execute/merge contracts, profile-queue node execution,
  and idempotent parent-state reconciliation retain strict sequential scheduling
- M25.2 bounded read-only fan-out is complete: opt-in replay-safe two-node waves,
  queue permits, transactional ordered merge, and isolated provider/scratch homes
  retain a shared read-only repository mount
- PR-native delivery fields with GitHub branch/draft-PR delivery integration
- full-text personal/project memory search with dashboard search results and memory-retrieval timeline visibility
- deterministic memory retrieval evaluation to separate full-text regressions from known semantic gaps
- reviewable memory proposal flow for curated corpus seeding, with Slice 5 unifying worker memory candidates and proposals through a memory-admission service plus a local library adoption spike
- dashboard visibility for TaskSpec, interactions, timeline events, logs, artifacts, replay controls, traces, memory, and tool inventory
- CI now measures Python coverage from `tests/unit` only and runs `tests/integration` as a separate pass
- pre-commit Ruff checks repo Python files for non-top-level imports while preserving a few intentional lazy imports in guarded modules
- shipped changes are tracked in [`CHANGELOG.md`](../CHANGELOG.md)

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- Codex/Antigravity now support native-agent defaults behind rollback flags, but deeper verifier/repair integration is still in progress
- Antigravity non-interactive runs use prompt-as-argv and permission/settings policy, so command logging and profile mapping need explicit redaction and tests
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Slices Only

1. M25.3: Temporal-only cutover and legacy retirement
   - Slice 1 — complete: persist `orchestration_runtime` on Task and WorkerRun, drain-gate dashboard widgets
   - Slice 2 — complete: Temporal default, fail-fast worker, graceful API 503, removed `CODE_AGENT_USE_TEMPORAL`, persisted cutover timestamp, dashboard drain metrics, and automated verification
   - Slice 3 — observation window: record the 14 Compose scenarios, then complete the 7-day active soak and ≥14-day/≥25-task retirement gate with task-class coverage
   - Slice 4 — legacy deletion: PR 4A removes dispatch (TaskQueueWorker, claims, leases); PR 4B removes LangGraph lifecycle
   - Slice 5 — schema cleanup: drop `lease_owner`, `lease_expires_at`, `next_attempt_at` after compatibility soak
2. M26: review comment repair
   - extend the PR repair loop from CI failures to actionable review feedback
   - may begin during M25.3 observation window

## Current Backlog

- Phase 4: decomposed task DAG, selective fan-out, review repair, and reliability-based autonomy policy.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

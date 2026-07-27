# Status

## Current Phase

Phase 4: selective autonomy after reliability.

Active focus:

- M25.3 Temporal-only cutover and legacy retirement.
  - Slice 3A and Slice 3B are complete by operator acceptance. The sole local
    Compose release environment retained the immutable cutover timestamp,
    deployed commit `251b9aa`, recovered a fan-out task after a worker restart
    during independent verification, reconciled Temporal and Postgres terminal
    state, and recorded clean drain plus rollback evidence in the immutable
    `m25.3-temporal-cutover-20260726T213001Z` release. See the
    [Slice 3 closeout](m25_3_slice_3_evidence_summary.md).
  - Slice 4A is complete (PR #340 retired the Postgres task scheduler,
    LangGraph lifecycle, and runtime selector).
  - Slice 4B is next for snapshot-backed schema cleanup.

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
- split API/worker runtime with transactional Temporal command dispatch
- Temporal workflow lifecycle with shared routing, approval, memory, verifier,
  review, and timeline domain callables
- worker adapters for Codex CLI, Antigravity CLI, and OpenRouter-backed execution
- sandboxed workspace/container execution with command artifact capture and retention controls
- skeptical memory + compact session state persistence
- orchestrator loads skeptical personal/project/session memory before worker dispatch and persists typed worker-produced memory after runs
- operational controls: task replay, approval decision endpoint, progress callbacks, and metrics
- generated TaskSpec contract for task goal/risk/type/delivery policy before worker routing
- repo registry and validation profiles gate public repo selection, protected paths, and validation defaults
- deterministic advisory repository memory profiles and skeptical memory retrieval/admission
- task decomposition into sequential or parallel DAG execution with durable node activity persistence and replay-safe worker fan-out
- PR-native delivery fields with GitHub branch/draft-PR delivery integration
- full-text personal/project memory search with dashboard search results and memory-retrieval timeline visibility
- deterministic memory retrieval evaluation to separate full-text regressions from known semantic gaps
- reviewable memory proposal flow for curated corpus seeding, memory-admission service, and episodic observation layer
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
- [high] resolving a non-permission interaction (clarification, review, merge)
  writes `TASK_SPEC_AND_ROUTE_GENERATED` as a catch-all timeline event, causing
  `classify_and_plan` to false-skip the entire ingestion/classification/routing
  pipeline on the next activity run — tasks resuming after a clarification cycle
  may proceed with uninitialized route and task spec metadata
  (`execution_interaction_service.py:L138` / `temporal/activities.py:L437`)
- [medium] `resolve_permission_escalation` deletes the Temporal state snapshot on
  rejection; if Temporal retries the activity the snapshot is gone, causing an
  unrecoverable `RuntimeError` retry loop until schedule-to-close timeout
  (`temporal/activities.py:L1495`)
- [medium] worker entrypoint uses bare `asyncio.run()` without a SIGTERM handler;
  container stop/pod termination kills the process without unwinding the
  `finally` block, leaking HTTP clients and interrupting Temporal activities
  (`apps/worker/main.py:L60`)

## Next Slices Only

1. M25.3 Slice 4B: snapshot-backed schema cleanup
   - remove the retained task lease columns after verifying migration and restore procedures
2. Temporal activity idempotency and interaction event fixes
   - use a dedicated timeline event type for non-permission interaction
     resolution instead of reusing `TASK_SPEC_AND_ROUTE_GENERATED`
   - make `resolve_permission_escalation` rejection idempotent by returning
     early when the snapshot is already deleted and the task is terminal
   - add SIGTERM signal handler to worker entrypoint for graceful container
     shutdown
3. M26: review comment repair
   - extend the PR repair loop from CI failures to actionable review feedback
   - may begin during the M25.3 evidence gate

## Current Backlog

- Phase 4: decomposed task DAG, selective fan-out, review repair, and reliability-based autonomy policy.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

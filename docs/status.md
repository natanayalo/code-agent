# Status

## Current Phase

Phase 4: selective autonomy after reliability.

Active focus:

- M26 review comment repair.
- M25.3 Temporal-only cutover and legacy retirement is complete:
  - Slice 4A retired the Postgres task scheduler, LangGraph lifecycle, and
    runtime selector in PR #340.
  - Slice 4B removed the task lease schema and WorkerNode load accounting after
    a snapshot-backed PostgreSQL rollback rehearsal. See the
    [Slice 4B evidence](m25_3_slice_4b_schema_evidence.md).

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

## Next Slices Only

1. M26: review comment repair
   - extend the PR repair loop from CI failures to actionable review feedback

## Current Backlog

- Phase 4: decomposed task DAG, selective fan-out, review repair, and reliability-based autonomy policy.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

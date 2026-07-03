# Status

## Current Phase

Phase 3: personal reliability before broader autonomy.

Active focus:

- Verify Phase 3 reliability metrics and prepare for Phase 4 selective autonomy

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
- PR-native delivery fields with GitHub branch/draft-PR delivery integration
- full-text personal/project memory search with dashboard search results and memory-retrieval timeline visibility
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

## Next Priorities

1. implement Milestone 23 Slice 3: semantic retrieval evaluation

## Current Backlog

Granular tasks for the active and upcoming milestones:

- Milestone 21: worker facade [x], runtime executor [x], sandbox adapter [x], and tool extraction.
- Milestone 23: memory load/persist [x], full-text memory search/retrieval visibility [x], semantic retrieval evaluation [ ].
- Phase 4: evaluate routing, semantic memory, DAG decomposition, selective fan-out, review repair, and autonomy policy only after Phase 3 proves reliability.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

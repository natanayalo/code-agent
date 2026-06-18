# Status

## Current Phase

Phase 2: bounded autonomy.

Active focus:

- Milestone 19 (Reflection and Improvement Pipeline) execution
- continue tightening native-agent observability and verifier acceptance policy

## Current Capabilities

- API + Telegram ingress for task intake
- shared-secret API auth for protected ingress routes
- durable Postgres persistence for users/sessions/tasks/runs/artifacts/memory
- split API/worker runtime with queue polling and lease claims
- LangGraph orchestrator with worker routing, approval checkpoints, verifier stage, and timeline persistence
- worker adapters for Codex CLI, Gemini CLI, and OpenRouter-backed execution
- sandboxed workspace/container execution with command artifact capture and retention controls
- skeptical memory + compact session state persistence
- operational controls: task replay, approval decision endpoint, progress callbacks, and metrics
- generated TaskSpec contract for task goal/risk/type/delivery policy before worker routing
- PR-native delivery fields with GitHub branch/draft-PR delivery integration
- dashboard visibility for TaskSpec, interactions, timeline events, logs, artifacts, replay controls, traces, memory, and tool inventory
- CI now measures Python coverage from `tests/unit` only and runs `tests/integration` as a separate pass
- pre-commit Ruff checks repo Python files for non-top-level imports while preserving a few intentional lazy imports in guarded modules
- shipped changes are tracked in [`CHANGELOG.md`](../CHANGELOG.md)

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- Codex/Gemini now support native-agent defaults behind rollback flags, but deeper verifier/repair integration is still in progress
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- OpenRouter remains useful for eval/raw-chat experiments but should be isolated as legacy tool-loop mode during the migration
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. continue tightening native-agent observability and verifier acceptance policy

## Current Backlog

Granular tasks for the active and upcoming milestones:

- T-195: Generate Scored Improvement Proposals.
- T-196: Dashboard UI for Reflection & Improvement Queue.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

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

- T-194: Capture execution friction from worker runtime.
- T-195: Generate Scored Improvement Proposals.
- T-196: Dashboard UI for Reflection & Improvement Queue.

## Recent Completed Milestones

- T-193: Integrate schemas with DB Proposal model (#236)
- T-192: Define Reflection and Improvement schemas.
- T-191: Add Trigger Sources: Schedule and Idle time.
- T-190: Dashboard UI for Idea Inbox (#227)
- T-189: Route Scout output to Review Inbox (#226)
- T-188: Implement Idea Inbox / Proposal store (#224)
- T-187: Add Read-Mostly sandbox policy (#223)
- T-186: Define Scout Mode task type and lane parameters.
- Prepare bounded-scout lane planning
- PR-native delivery fields and GitHub branch/draft-PR integration
- Milestone 17.5: Full E2E Stabilization (T-164 to T-178)
- Milestone 17: Native Agent Worker Runtime Profiles (T-140 to T-163)
- Milestone 10: Telegram ingress and progress update flow (T-050 to T-053)
- Milestone 11: tool wrappers and MCP compatibility slices (T-080 to T-089, T-107)
- Milestone 12: observability + replay (T-090 to T-092)
- Milestone 13 (remainder): hardening controls including auth/safety/budget/retention (T-100 to T-105)
- Milestone 14 baseline: planning/context/review intelligence slices (T-106, T-108 to T-112, T-114 to T-128)
- Milestone 15: product identity and documentation refresh
- Milestone A (TaskSpec foundation and human workflow)
- Milestone 16: operator UX (dashboard/PWA) + observability (OTEL/OpenInference tracing & traces in dashboard) + working context/memory/tool inventory UIs

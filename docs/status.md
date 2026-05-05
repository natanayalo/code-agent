# Status

## Current Phase

Phase 1: clarity and control.

Active focus:

- Milestone 17 (native agent worker runtime profiles)
- migration planning for Codex/Gemini native-agent execution
- preserving TaskSpec, HumanInteraction, dashboard, and verification governance around worker autonomy

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
- dashboard visibility for TaskSpec, interactions, timeline events, logs, artifacts, replay controls, traces, memory, and tool inventory
- CI now measures Python coverage from `tests/unit` only and runs `tests/integration` as a separate pass
- pre-commit Ruff checks repo Python files for non-top-level imports while preserving a few intentional lazy imports in guarded modules

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- TaskSpec can surface clarification/permission needs and sync HumanInteraction rows, but the graph does not yet halt on clarification before worker dispatch
- PR-native delivery is represented as desired delivery metadata only; branch/PR creation is still a future slice
- Codex/Gemini still run through the shared operation-selector `CliRuntimeLoop`, which can waste budget on repeated JSON tool-call turns instead of using native coding-agent loops
- profile-aware routing now supports the initial capability matrix, but native-agent defaults and deeper verifier/repair integration are still in progress
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- OpenRouter remains useful for eval/raw-chat experiments but should be isolated as legacy tool-loop mode during the migration
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. convert Codex, then Gemini, to native-agent mode behind rollback flags (T-155, T-156)
2. add clarification dispatch gating and independent verifier/repair flow (T-157 to T-159)
3. isolate OpenRouter as opt-in legacy tool-loop mode and add native-agent parity/eval coverage (T-160 to T-162)
4. add PR-native delivery fields and GitHub branch/draft-PR integration after native worker delivery is stable

## Current Backlog

Granular tasks for the active and upcoming milestones:

### Milestone 17: Native Agent Worker Runtime Profiles
- [ ] T-156: convert Gemini worker to native-agent default behind `CODE_AGENT_GEMINI_RUNTIME_MODE`
- [ ] T-157: add a clarification gate before worker routing/dispatch when TaskSpec requires clarification
- [ ] T-158: add an independent verifier execution stage with read-only/default-safe behavior
- [ ] T-159: add bounded continuation/repair after verifier failure
- [ ] T-160: add optional LLM orchestrator brain for TaskSpec enrichment, classification, clarification, profile recommendation, retry/escalation, and verifier acceptance
- [ ] T-161: update observability/artifact persistence for runtime mode, profile, CLI stdout/stderr/events, final message, diff, changed files, and verifier result
- [ ] T-162: deprecate operation-selector mode for Codex/Gemini while keeping `CliRuntimeLoop` for raw chat/OpenRouter compatibility

### Milestone 17 Done (Published)
- [x] T-155: convert Codex worker to native-agent default behind `CODE_AGENT_CODEX_RUNTIME_MODE` ([#160](https://github.com/natanayalo/code-agent/pull/160))
- [x] T-154: add a native agent runner abstraction for one-shot CLI task-packet execution, final message capture, diff/files/artifact collection, and timeout/error handling ([#159](https://github.com/natanayalo/code-agent/pull/159))
- [x] T-140: define `WorkerRuntimeMode`, `WorkerProfile`, capability tags, and permission-profile vocabulary ([#154](https://github.com/natanayalo/code-agent/pull/154))
- [x] T-141: replace heuristic worker routing with profile-aware selection logic in the orchestrator ([#155](https://github.com/natanayalo/code-agent/pull/155))
- [x] T-142: map existing workers to Codex native executor, Gemini native planner/reviewer/executor, and OpenRouter legacy tool-loop profiles ([#158](https://github.com/natanayalo/code-agent/pull/158))

## Recent Completed Milestones

- Milestone 10: Telegram ingress and progress update flow (T-050 to T-053)
- Milestone 11: tool wrappers and MCP compatibility slices (T-080 to T-089, T-107)
- Milestone 12: observability + replay (T-090 to T-092)
- Milestone 13 (remainder): hardening controls including auth/safety/budget/retention (T-100 to T-105)
- Milestone 14 baseline: planning/context/review intelligence slices (T-106, T-108 to T-112, T-114 to T-128)
- Milestone 15: product identity and documentation refresh
- Milestone A (TaskSpec foundation and human workflow)
- Milestone 16: operator UX (dashboard/PWA) + observability (OTEL/OpenInference tracing & traces in dashboard) + working context/memory/tool inventory UIs

# Status

## Current Phase

Phase 1: clarity and control.

Active focus:

- Milestone 17.5 (e2e stabilization; see granular backlog for component tasks)
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
- PR-native delivery is represented as desired delivery metadata only; branch/PR creation is still a future slice
- Codex/Gemini now support native-agent defaults behind rollback flags, but deeper verifier/repair integration is still in progress
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- OpenRouter remains useful for eval/raw-chat experiments but should be isolated as legacy tool-loop mode during the migration
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. stabilize full e2e path for dashboard, tracing, API, orchestrator, and worker runtime (T-164 to T-171)
2. continue tightening native-agent observability and verifier acceptance policy while Milestone 17.5 stabilization work is in progress
3. add PR-native delivery fields and GitHub branch/draft-PR integration after completion of related backlog items
4. prepare bounded-scout lane planning after Milestone 17.5 stabilization

## Current Backlog

Granular tasks for the active and upcoming milestones:

### Milestone 17: Native Agent Worker Runtime Profiles

### Milestone 17.5: Full E2E Stabilization
- Milestone target: increase test coverage across all e2e-critical slices (T-164 to T-171) with explicit unit/integration/dashboard tests for each changed behavior, while meeting existing CI coverage gates.
See [Stabilization Tasks](stabilization_tasks.md) for the full list of tasks.

- [ ] T-174: Implement deterministic-first verification and reclassify infra timeouts
- [ ] T-175: Add infra-failure (shell crash) detection to NativeAgentRunner
- [ ] T-176: Standardize Phoenix/OpenInference span attributes and JSON payloads
- [ ] T-177: Optimize discovery latency and brain-router fallback resilience
- [ ] T-178: E2E Forensic Investigation & Runtime Hardening (404 polling, JSON parsing resilience)
- [ ] T-171: local e2e runbook + compose/env verification

### Milestone 17.5 Done (Published)
- [x] T-173: Simplify native-agent prompts and enforce delivery_mode (Review vs Fix) ([#179](https://github.com/natanayalo/code-agent/pull/179)) — reduced prompt size by 30%+, refactored role/permissions for "read" vs "read/write" execution workers, and aligned native instructions with summary/workspace modes.
- [x] T-164: native runner contract repair ([#172](https://github.com/natanayalo/code-agent/pull/172)) — normalized final-message and error extraction in NativeAgentRunner, refactored GeminiCliWorker to use common outputs, and expanded integration coverage.
- [x] T-166: interaction response state machine hardening ([#174](https://github.com/natanayalo/code-agent/pull/174)) — fixed missing imports, corrected graph data structure inconsistencies for hashing, and repaired timeline emission.

- [x] T-165: cancellation semantics hardening ([#173](https://github.com/natanayalo/code-agent/pull/173)) — enforced terminal status machine (FAILED for cancelled), added heartbeat-race abort guards, and ensured idempotent TASK_CANCELLED timeline event recording.
- [x] T-172: codex native Docker sandbox-boundary alignment — documented and enforced runtime policy for containerized native runs where Codex Linux sandbox may be unavailable; using Docker as the primary boundary and running Codex with `--sandbox danger-full-access` in-container only for trusted repos matched via operator-controlled patterns.

- [x] T-167: integration coverage expansion for existing cancel/interaction endpoints — added comprehensive integration tests for cancellation atomicity, terminality, and interaction cleanup.
- [x] T-168: migration parity for task_cancelled timeline event — implemented migration for the new event type and verified upgrade path/constraint integrity.
- [x] T-170: tracing/observability guardrails for native runs
- [x] T-169: dashboard interaction/cancel UX stabilization ([#175](https://github.com/natanayalo/code-agent/pull/175)) — added task-detail interaction resolve/cancel operator controls with resilient API/error handling and dashboard test coverage.

### Milestone 17 Done (Published)
- [x] T-163: add brain-driven retry/escalation and verifier-acceptance hints as first-class, clamp-governed controls in the orchestration graph ([#170](https://github.com/natanayalo/code-agent/pull/170)) — added explicit brain hint contracts, deterministic route/verification clamps, and timeline-visible rationale for applied vs ignored hints.
- [x] T-162: deprecate operation-selector mode for Codex/Gemini while keeping `CliRuntimeLoop` for raw chat/OpenRouter compatibility ([#169](https://github.com/natanayalo/code-agent/pull/169)) — hard-pinned Codex/Gemini defaults to native-agent mode, added explicit legacy tool-loop profile opt-in with per-task `worker_profile_override`, and expanded deprecation observability via warning logs plus runtime-mode/legacy usage metrics.
- [x] T-161: update observability/artifact persistence for runtime mode, profile, CLI stdout/stderr/events, final message, diff, changed files, and verifier result ([#168](https://github.com/natanayalo/code-agent/pull/168)) — surfaced `latest_run.files_changed` via task snapshots and added dashboard run-observability rendering for worker/profile/runtime/verification details.
- [x] T-160: add optional LLM orchestrator brain for TaskSpec enrichment, classification, and clarification ([#167](https://github.com/natanayalo/code-agent/pull/167)) — implemented async model-backed enrichment with strict safety clamps, rule-based fallbacks, and 93% test coverage.
- [x] T-159: add bounded continuation/repair after verifier failure ([#164](https://github.com/natanayalo/code-agent/pull/164))
- [x] T-158: add an independent verifier execution stage with read-only/default-safe behavior ([#163](https://github.com/natanayalo/code-agent/pull/163))
- [x] T-157: add a clarification gate before worker routing/dispatch when TaskSpec requires clarification ([#162](https://github.com/natanayalo/code-agent/pull/162))
- [x] T-156: convert Gemini worker to native-agent default behind `CODE_AGENT_GEMINI_RUNTIME_MODE` ([#161](https://github.com/natanayalo/code-agent/pull/161))
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

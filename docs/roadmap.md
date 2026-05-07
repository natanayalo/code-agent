# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Gemini/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 1: clarity and control.

Priority sequence:

1. Milestone 15: Product identity and documentation refresh
2. Milestone A: TaskSpec and human workflow foundation
3. Milestone 16: Operator UX and transparency
4. Milestone 17: Worker profile and runtime leverage
5. Milestone 17.5: Full E2E stabilization

Planned next phases:

1. Phase 2: bounded autonomy
2. Phase 3: deeper platform maturity

## Milestone 15: Product Identity and Documentation Refresh

Goal:

- align public and internal docs with current platform behavior and near-term direction

Deliverables:

- rewritten `README.md`
- refreshed `docs/architecture.md`
- new `docs/runbook.md`
- new `docs/roadmap.md`
- concise `docs/status.md`

Status:

- completed (documentation synchronized with behavior)

## Milestone A: TaskSpec and Human Workflow Foundation

Goal:

- establish a structured task contract and first-class human checkpoints before expanding worker autonomy

Planned deliverables:

- persisted `TaskSpec` with goal, task type, risk, acceptance criteria, policy, verification, and delivery metadata
- deterministic TaskSpec generation before worker routing
- task status API and dashboard visibility for TaskSpec
- generic HumanInteraction records for clarification, permission, final review, merge approval, and blocked help
- compatibility path from existing approval endpoints to HumanInteraction

Non-goals:

- branch/PR creation
- merge automation
- scout/autonomy expansion
- replacing worker-profile routing in this milestone

Success criteria:

- every worker run starts from an inspectable task contract
- ambiguous and high-risk tasks expose the precise human checkpoint needed
- dashboard/operator views can prioritize tasks needing input

## Milestone 16: Operator UX and Transparency

Goal:

- provide richer task inspection and intervention than Telegram-only flows

Planned deliverables:

- thin local dashboard/PWA
- task list + task detail views
- runtime stage, active worker, and status visualization
- command timeline, changed files, and artifact visibility
- approval inbox and explicit operator controls
- retry/replay/cancel actions from UI
- push-style notifications for approval needed, completion, failure, and ideas

Non-goals:

- native iOS/Android app
- replacing chat as intent interface
- broad multi-user admin console

Success criteria:

- active task inspection without direct log reading
- faster/clearer approval flow than Telegram-only
- replay/retry invokable from dashboard

## Milestone 17: Native Agent Worker Runtime Profiles

Goal:

- migrate Codex and Gemini from platform-driven operation selection into native autonomous coding-agent workers while keeping orchestration, sandboxing, approval, budgets, verification, retries, artifacts, and human handoff under platform control

Non-goals:

- removing safety/governance boundaries
- giving native agents host-level or deployment permissions by default
- rebuilding Codex/Gemini inner loops in the platform
- broad multi-agent swarms or autonomous merge/deploy
- removing raw chat/tool-loop support before a replacement path is proven

Design principles:

- keep the current architecture and refactor inside existing worker/orchestrator boundaries
- move control from each thought/action step to the boundary around a native agent run
- treat native CLI workers as bounded repo executors, not as orchestrator owners
- preserve `WorkerRequest`/`WorkerResult` as the platform contract
- keep deterministic clamps for approvals, permissions, risk, worker availability, retry limits, secrets, sandbox mode, network, and budget caps
- make runtime mode explicit and observable in task details, worker runs, traces, and artifacts

Core design:

- add `WorkerRuntimeMode`: `native_agent`, `tool_loop`, `planner_only`, `reviewer_only`
- add `WorkerProfile`: profile name, worker type, runtime mode, capability tags, default budgets, permission profile, mutation policy, self-review policy, and supported delivery modes
- make Codex and Gemini `native_agent` by default once parity/evals pass
- keep `CliRuntimeLoop` as `tool_loop` infrastructure for OpenRouter/raw chat models and targeted compatibility tests
- isolate OpenRouter as legacy `tool_loop`, disabled by default unless explicitly configured for evaluation
- add an optional LLM orchestrator brain for TaskSpec enrichment, clarification detection, worker/profile recommendation, retry/escalation recommendation, and verifier-result acceptance; deterministic policy remains authoritative

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies |
| --- | --- | --- | --- | --- | --- | --- |
| T-140 | P0 | Define worker runtime/profile contracts. | Add `WorkerRuntimeMode` and `WorkerProfile` models plus capability tags and permission-profile vocabulary. Keep compatibility with existing `WorkerType`. | Profiles validate in tests; profile JSON can describe Codex native, Gemini native, OpenRouter tool-loop, planner-only, and reviewer-only profiles. | `workers/base.py`, `orchestrator/state.py`, `tests/unit/test_worker_interface.py` | Avoid schema churn until persistence needs are clear. |
| T-141 | P0 | Replace heuristic worker routing with profile-aware selection. | Route by TaskSpec, constraints, availability, profile mode, mutation policy, and delivery mode. Preserve manual override behavior. | Existing route tests pass; new tests cover native default selection, unavailable profile failure, and OpenRouter legacy opt-in. | `orchestrator/graph.py`, `orchestrator/execution.py`, `apps/api/task_service_factory.py`, graph tests | Must keep explicit failure when requested worker/profile is unavailable. |
| T-142 | P0 | Map existing workers into the capability matrix. | Start with Codex native executor, Gemini native planner/reviewer/executor, OpenRouter legacy tool-loop. | Dashboard/API can expose selected worker/profile/runtime mode through existing snapshots or metadata. | `docs/status.md`, `docs/architecture.md`, `orchestrator/state.py`, task API tests | Dashboard typing may need a small follow-up. |
| T-154 | P0 | Add native agent runner abstraction. | Create a reusable boundary runner that invokes a CLI once per task packet inside the sandbox and collects final message, JSONL/events when available, exit code, stdout/stderr artifacts, diff, changed files, and verification hints. | Unit tests prove timeout, non-zero exit, final-message parsing, and diff/artifact collection without using real CLIs. | `workers/native_agent_runner.py`, `workers/codex_cli_worker.py`, `workers/gemini_cli_worker.py`, `workers/base.py`, worker tests | Native CLI event formats vary; start with final message + git diff as the stable contract. |
| T-155 | P0 | Convert Codex worker to native-agent default behind a flag. | Use `codex exec` as the inner loop with platform-controlled cwd, sandbox mode, model/profile, timeout, final-message capture, and optional JSONL event capture. Keep the current operation-selector implementation as `tool_loop`. | Codex native path can inspect/edit/test in a fixture repo and returns `WorkerResult` with summary, files changed, diff, artifacts, budget usage, and failure kind. | `workers/codex_cli_worker.py`, `workers/codex_exec_adapter.py`, `apps/api/task_service_factory.py`, Codex worker tests, integration fixture tests | Need careful CLI sandbox/approval mapping; command-level audit may be coarser unless JSONL events are parsed. |
| T-156 | P0 | Convert Gemini worker to native-agent default behind a flag. | Use Gemini CLI non-interactive prompt mode with sandboxing, output-format support, final message capture, and explicit no-network/no-secret defaults. Keep operation-selector mode as `tool_loop`. | Gemini native path has parity tests matching Codex native worker contract and cleanly reports auth/provider errors. | `workers/gemini_cli_worker.py`, `workers/gemini_cli_adapter.py`, `apps/api/task_service_factory.py`, Gemini worker tests | Gemini sandbox expansion/approval behavior must not bypass platform approvals. |
| T-157 | P0 | Add clarification gate before dispatch. | If TaskSpec requires clarification, halt before worker routing/dispatch and persist a resumable HumanInteraction. Existing TaskSpec rows already map to interactions; graph must honor them. | Ambiguous tasks do not dispatch a worker; dashboard/API show pending clarification; replay/resume path is documented. | `orchestrator/graph.py`, `orchestrator/execution.py`, `repositories/sqlalchemy.py`, interaction/task tests | Needs a clear operator response path before broad rollout. |
| T-158 | P1 | Add independent verifier execution stage. | Run TaskSpec verification commands or a read-only verifier profile after native worker completion in a fresh/reused sandbox boundary, separate from worker self-review. | Verifier can pass/fail/warn independently; failures include actionable `failure_kind` and artifacts; no mutation by default. | `orchestrator/graph.py`, `orchestrator/review.py` or new `orchestrator/verification.py`, verifier tests | Verification commands can be expensive; must respect global budget caps. |
| T-159 | P1 | Add continuation/repair after verifier failure. | Extend existing review repair handoff into a bounded verifier repair flow using the same workspace when safe, capped by retry/repair budgets. | One repair attempt can be dispatched after verifier failure; repeated failures stop with human-readable handoff. | `orchestrator/graph.py`, `orchestrator/review.py`, graph tests | Avoid infinite loops and cross-worker workspace confusion. |
| T-160 | P1 | Add optional LLM orchestrator brain for TaskSpec enrichment. | Introduce async model-backed enrichment for assumptions, criteria, non-goals, classification, and clarification. Planner-style reasoning runs in read-only mode. | Suggestions merged into TaskSpec; rule-based fallbacks on timeout/error; 90%+ coverage. | `orchestrator/brain.py`, `orchestrator/graph.py`, `orchestrator/task_spec.py` | Adds provider latency; requires async graph transition. |
| T-163 | P2 | Add brain-driven policy hints. | Add first-class controls for brain-suggested retry/escalation routing and verifier acceptance logic. Ensure deterministic clamps remain authoritative. | Brain hints influence retry selection and verifier outcomes; rationale is observable in the decision chain. | `orchestrator/graph.py`, `orchestrator/verification.py`, `orchestrator/execution.py` | Complexity in balancing model hints with safety policy. |
| T-161 | P1 | Update observability and artifacts for native agents. | Persist runtime mode/profile, CLI command metadata, stdout/stderr/event artifacts, final message, diff, changed files, and verifier result links. | Task detail can explain what native agent ran, for how long, what changed, and what verification accepted/rejected. | `orchestrator/execution.py`, `db/models.py` if needed, dashboard task detail, artifact tests | Prefer metadata-only changes unless persistence schema is necessary. |
| T-162 | P2 | Deprecate operation-selector mode for native CLIs. | Keep `CliRuntimeLoop` for raw chat/OpenRouter and tests. Add warnings/metrics when Codex/Gemini run in `tool_loop`; remove default routing to those modes after evals. | Codex/Gemini default to `native_agent`; OpenRouter remains isolated legacy `tool_loop`; docs explain override and rollback. | `docs/architecture.md`, `docs/runbook.md`, worker bootstrap/tests | Do not delete the old path until rollback and eval coverage are proven. |

Recommended implementation order:

1. T-140, T-142, T-141
2. T-154, T-155
3. T-156
4. T-157
5. T-158, T-159
6. T-160
7. T-161, T-162

Feature flags and rollout controls:

- `CODE_AGENT_WORKER_PROFILES_ENABLED`: profile-aware routing
- `CODE_AGENT_CODEX_RUNTIME_MODE`: `native_agent` or `tool_loop`
- `CODE_AGENT_GEMINI_RUNTIME_MODE`: `native_agent` or `tool_loop`
- `CODE_AGENT_OPENROUTER_ENABLED`: default `false` outside eval/legacy environments
- `CODE_AGENT_ORCHESTRATOR_BRAIN_ENABLED`: default `false`
- `CODE_AGENT_NATIVE_AGENT_EVENT_CAPTURE_ENABLED`: parse CLI JSONL/events when available
- `CODE_AGENT_INDEPENDENT_VERIFIER_ENABLED`: default `false` until verifier budget behavior is proven

Tests and evals to add:

- unit tests for profile validation, profile selection, and deterministic policy clamps
- native-runner unit tests with fake Codex/Gemini binaries for success, timeout, auth/provider failure, malformed output, and no-change success
- integration fixture where native Codex/Gemini fixes a small failing test in an isolated workspace
- regression eval for the known failure mode: repeated read-only JSON `tool_call` actions must not happen in native mode
- verifier evals for success, no tests reported, failing verification command, and repair handoff
- artifact/observability tests proving final message, diff, files changed, runtime mode, profile, and verifier outcome persist

Migration/deprecation plan:

- phase 1: introduce profiles and keep all current worker behavior as the default
- phase 2: enable Codex native-agent mode in non-production/dev with fake-binary and fixture coverage
- phase 3: enable Gemini native-agent mode in non-production/dev
- phase 4: run side-by-side evals comparing native-agent vs operation-selector behavior on representative tasks
- phase 5: make Codex/Gemini native-agent default, retain per-worker rollback flags
- phase 6: isolate OpenRouter as legacy tool-loop mode and keep disabled by default unless explicitly configured
- phase 7: remove Codex/Gemini operation-selector defaults only after a documented rollback path and retained compatibility tests exist

## Milestone 17.5: Full E2E Stabilization

Goal:

- make end-to-end execution reliable across dashboard, tracing, API, orchestrator, and worker runtime

Non-goals:

- feature expansion beyond stabilization and operator controls
- autonomy expansion or scout-mode changes
- infrastructure redesign outside reliability-focused fixes

Deliverables:

- task lifecycle and control-path reliability (cancel, interaction response, resume semantics)
- API and UI operator controls with integration coverage
- migration correctness for timeline and interaction-related states
- observability/tracing clarity for native runtime executions
- runnable local e2e path with compose/env and runbook verification

Coverage target:

- improve test coverage for every Milestone 17.5 behavior change by adding targeted tests in the relevant layer (`tests/unit`, `tests/integration`, and dashboard Vitest suites where applicable)
- treat coverage as a release gate for this milestone: no slice is complete without new/updated tests that exercise the changed path
- meet existing CI coverage gates while raising coverage depth on e2e-critical flows (cancel, interactions, migration parity, tracing, and local e2e wiring)

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies |
| --- | --- | --- | --- | --- | --- | --- |
| T-164 | P0 | Repair native runner contracts and output parsing reliability. | Normalize final-message parsing, environment propagation, and summary fallback behavior for native runs. | Native runner and Gemini/Codex native tests pass with deterministic summary and stderr handling. | `workers/native_agent_runner.py`, `workers/gemini_cli_worker.py`, `workers/codex_cli_worker.py`, worker unit tests | Cross-provider output differences can hide regressions without targeted fixtures. |
| T-165 | P0 | Harden cancellation semantics across queue/execution races. | Ensure cancellation remains terminal/sticky while leases and concurrent completion paths resolve. | Cancelled tasks do not requeue or transition back to running/completed unexpectedly. | `orchestrator/execution.py`, `repositories/sqlalchemy.py`, task execution tests | Concurrency paths are timing-sensitive and require robust coverage. |
| T-166 | P0 | Harden interaction response state-machine behavior. | Enforce valid transitions and safe resume gating after human responses. | Interaction responses are idempotent/conflict-safe and only resume when policy allows. | `orchestrator/execution.py`, `repositories/sqlalchemy.py`, interaction tests | Clarification and permission flows can diverge without explicit transition guards. |
| T-167 | P0 | Add integration coverage for cancel/interaction endpoints. | Cover success, not-found, conflict, and idempotent paths for new endpoints. | Integration tests validate endpoint behavior against real service wiring. | `apps/api/routes/tasks.py`, `tests/integration/test_task_endpoints.py` | Missing coverage can mask regressions in operator workflows. |
| T-168 | P0 | Align DB migration constraints with task cancellation timeline events. | Add migration updates for `task_cancelled` timeline check constraints and validate upgrade path behavior. | Migrated databases accept `task_cancelled` timeline writes without constraint failures. | `db/migrations/versions/*`, `tests/integration/test_db_migrations.py` | Constraint drift across revisions can break production-upgraded databases. |
| T-169 | P1 | Stabilize dashboard interaction/cancel UX behavior. | Ensure component typing, response rendering, and action states match API semantics. | Dashboard tests cover interaction response and cancel UX paths without runtime/type regressions. | `dashboard/src/components/*`, `dashboard/src/services/api.ts`, dashboard tests | UI state drift from backend semantics can confuse operators. |
| T-170 | P1 | Add tracing/observability guardrails for native runs. | Bound trace payload size/noise while preserving actionable runtime metadata and status signals. | Native-run spans remain readable, structured, and policy-safe under heavy output. | `apps/observability.py`, `workers/native_agent_runner.py`, tracing tests | Overly noisy spans reduce debuggability and increase telemetry overhead. |
| T-171 | P1 | Verify local e2e execution path and runbook/compose alignment. | Tighten local environment defaults, compose wiring checks, and operational runbook guidance. | Local e2e smoke path is reproducible with documented setup and verification steps. | `docker-compose.yml`, `docs/runbook.md`, infra tests/scripts | Environment skew between API/worker/dashboard/tracing can cause false negatives. |

## Milestone 18: Controlled Autonomy / Scout Mode

Goal:

- add bounded proactive exploration without destabilizing primary execution

Planned deliverables:

- separate scout mode lane, queue, and budget policy
- read-mostly default permissions
- idea inbox/proposal store
- trigger sources: schedule, idle time, manual prompts, recurring failure signals

Required controls:

- explicit budget cap
- no direct production mutation
- output routed to review inbox only

## Milestone 19: Reflection and Improvement Pipeline

Goal:

- convert execution friction into structured, reviewable improvement proposals

Planned deliverables:

- friction report schema
- improvement suggestion schema
- proposal scoring/planning by value, effort, risk, layer impact, validation path, and HITL need
- review queue for improvement proposals

Manual-only zones:

- auth/security
- secrets/sandbox boundaries
- approval core logic
- deployment/billing controls

## Milestone 20: Operational Self-Awareness

Goal:

- make runtime identity, constraints, and maintenance paths explicit to workers/operators

Planned deliverables:

- environment manifest (identity/build/runtime/worker/tool/approval capabilities)
- agent-visible maintenance request actions (restart, recycle worker, reload config, dependency refresh, operator attention)
- explicit forbidden action declarations

Control rule:

- agent can request privileged maintenance actions; operator/system policy decides execution

## Milestone 21: Worker Runtime Hotspot Refactor

Goal:

- reduce maintenance risk in runtime hotspots via incremental internal boundary extraction

Planned internal splits:

- worker facade
- runtime executor
- sandbox/session adapter
- prompt assembler
- tool execution and permission gate
- post-run pipeline
- result mapper

Approach:

- incremental extraction
- preserve existing contracts and behavior
- prioritize testability and reviewability

## Phase Sequencing Summary

Phase 1:

1. Milestone 15
2. Milestone A
3. Milestone 16
4. Milestone 17

Phase 2:

1. Milestone 18
2. Milestone 19

Phase 3:

1. Milestone 20
2. Milestone 21

## Open Planning Questions

1. which public product sentence remains canonical after milestone A rollout?
2. which runtime owns planning by default in production policy?
3. should scout mode launch as strictly read-only first?
4. which proposal categories (if any) can be auto-promoted?
5. which maintenance actions are request-only vs executable?
6. which hotspot refactors are highest leverage before autonomy expansion?

# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Gemini/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 2: bounded autonomy.

Priority sequence:

1. Milestone 18: Controlled Autonomy / Scout Mode
2. Milestone 19: Reflection and Improvement Pipeline
3. Milestone 19.5: Gemini to Antigravity Migration

Planned next phases:

1. Phase 3: deeper platform maturity

Past phases:

1. Phase 1: clarity and control (Milestones 15 through 17.5)

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
- dashboard/operator views can prioritize tasks needing input

Status:

- completed

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

Status:

- completed

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

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-140 | P0 | Define worker runtime/profile contracts. | Add `WorkerRuntimeMode` and `WorkerProfile` models plus capability tags and permission-profile vocabulary. Keep compatibility with existing `WorkerType`. | Profiles validate in tests; profile JSON can describe Codex native, Gemini native, OpenRouter tool-loop, planner-only, and reviewer-only profiles. | `workers/base.py`, `orchestrator/state.py`, `tests/unit/test_worker_interface.py` | Avoid schema churn until persistence needs are clear. | (Done) |
| T-141 | P0 | Replace heuristic worker routing with profile-aware selection. | Route by TaskSpec, constraints, availability, profile mode, mutation policy, and delivery mode. Preserve manual override behavior. | Existing route tests pass; new tests cover native default selection, unavailable profile failure, and OpenRouter legacy opt-in. | `orchestrator/graph.py`, `orchestrator/execution.py`, `apps/api/task_service_factory.py`, graph tests | Must keep explicit failure when requested worker/profile is unavailable. | (Done) |
| T-142 | P0 | Map existing workers into the capability matrix. | Start with Codex native executor, Gemini native planner/reviewer/executor, OpenRouter legacy tool-loop. | Dashboard/API can expose selected worker/profile/runtime mode through existing snapshots or metadata. | `docs/status.md`, `docs/architecture.md`, `orchestrator/state.py`, task API tests | Dashboard typing may need a small follow-up. | (Done) |
| T-154 | P0 | Add native agent runner abstraction. | Create a reusable boundary runner that invokes a CLI once per task packet inside the sandbox and collects final message, JSONL/events when available, exit code, stdout/stderr artifacts, diff, changed files, and verification hints. | Unit tests prove timeout, non-zero exit, final-message parsing, and diff/artifact collection without using real CLIs. | `workers/native_agent_runner.py`, `workers/codex_cli_worker.py`, `workers/gemini_cli_worker.py`, `workers/base.py`, worker tests | Native CLI event formats vary; start with final message + git diff as the stable contract. | (Done) |
| T-155 | P0 | Convert Codex worker to native-agent default behind a flag. | Use `codex exec` as the inner loop with platform-controlled cwd, sandbox mode, model/profile, timeout, final-message capture, and optional JSONL event capture. Keep the current operation-selector implementation as `tool_loop`. | Codex native path can inspect/edit/test in a fixture repo and returns `WorkerResult` with summary, files changed, diff, artifacts, budget usage, and failure kind. | `workers/codex_cli_worker.py`, `workers/codex_exec_adapter.py`, `apps/api/task_service_factory.py`, Codex worker tests, integration fixture tests | Need careful CLI sandbox/approval mapping; command-level audit may be coarser unless JSONL events are parsed. | (Done) |
| T-156 | P0 | Convert Gemini worker to native-agent default behind a flag. | Use Gemini CLI non-interactive prompt mode with sandboxing, output-format support, final message capture, and explicit no-network/no-secret defaults. Keep operation-selector mode as `tool_loop`. | Gemini native path has parity tests matching Codex native worker contract and cleanly reports auth/provider errors. | `workers/gemini_cli_worker.py`, `workers/gemini_cli_adapter.py`, `apps/api/task_service_factory.py`, Gemini worker tests | Gemini sandbox expansion/approval behavior must not bypass platform approvals. | (Done) |
| T-157 | P0 | Add clarification gate before dispatch. | If TaskSpec requires clarification, halt before worker routing/dispatch and persist a resumable HumanInteraction. Existing TaskSpec rows already map to interactions; graph must honor them. | Ambiguous tasks do not dispatch a worker; dashboard/API show pending clarification; replay/resume path is documented. | `orchestrator/graph.py`, `orchestrator/execution.py`, `repositories/sqlalchemy.py`, interaction/task tests | Needs a clear operator response path before broad rollout. | (Done) |
| T-158 | P1 | Add independent verifier execution stage. | Run TaskSpec verification commands or a read-only verifier profile after native worker completion in a fresh/reused sandbox boundary, separate from worker self-review. | Verifier can pass/fail/warn independently; failures include actionable `failure_kind` and artifacts; no mutation by default. | `orchestrator/graph.py`, `orchestrator/review.py` or new `orchestrator/verification.py`, verifier tests | Verification commands can be expensive; must respect global budget caps. | (Done) |
| T-159 | P1 | Add continuation/repair after verifier failure. | Extend existing review repair handoff into a bounded verifier repair flow using the same workspace when safe, capped by retry/repair budgets. | One repair attempt can be dispatched after verifier failure; repeated failures stop with human-readable handoff. | `orchestrator/graph.py`, `orchestrator/review.py`, graph tests | Avoid infinite loops and cross-worker workspace confusion. | (Done) |
| T-160 | P1 | Add optional LLM orchestrator brain for TaskSpec enrichment. | Introduce async model-backed enrichment for assumptions, criteria, non-goals, classification, and clarification. Planner-style reasoning runs in read-only mode. | Suggestions merged into TaskSpec; rule-based fallbacks on timeout/error; 90%+ coverage. | `orchestrator/brain.py`, `orchestrator/graph.py`, `orchestrator/task_spec.py` | Adds provider latency; requires async graph transition. | (Done) |
| T-163 | P2 | Add brain-driven policy hints. | Add first-class controls for brain-suggested retry/escalation routing and verifier acceptance logic. Ensure deterministic clamps remain authoritative. | Brain hints influence retry selection and verifier outcomes; rationale is observable in the decision chain. | `orchestrator/graph.py`, `orchestrator/verification.py`, `orchestrator/execution.py` | Complexity in balancing model hints with safety policy. | (Done) |
| T-161 | P1 | Update observability and artifacts for native agents. | Persist runtime mode/profile, CLI command metadata, stdout/stderr/event artifacts, final message, diff, changed files, and verifier result links. | Task detail can explain what native agent ran, for how long, what changed, and what verification accepted/rejected. | `orchestrator/execution.py`, `db/models.py` if needed, dashboard task detail, artifact tests | Prefer metadata-only changes unless persistence schema is necessary. | (Done) |
| T-162 | P2 | Deprecate operation-selector mode for native CLIs. | Keep `CliRuntimeLoop` for raw chat/OpenRouter and tests. Add warnings/metrics when Codex/Gemini run in `tool_loop`; remove default routing to those modes after evals. | Codex/Gemini default to `native_agent`; OpenRouter remains isolated legacy `tool_loop`; docs explain override and rollback. | `docs/architecture.md`, `docs/runbook.md`, worker bootstrap/tests | Do not delete the old path until rollback and eval coverage are proven. | (Done) |

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

Status:

- completed (native agent defaults shipped and stabilized in Milestone 17.5)

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

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-164 | P0 | Repair native runner contracts and output parsing reliability. | Normalize final-message parsing, environment propagation, and summary fallback behavior for native runs. | Native runner and Gemini/Codex native tests pass with deterministic summary and stderr handling. | `workers/native_agent_runner.py`, `workers/gemini_cli_worker.py`, `workers/codex_cli_worker.py`, worker unit tests | Cross-provider output differences can hide regressions without targeted fixtures. | (Done) |
| T-165 | P0 | Harden cancellation semantics across queue/execution races. | Ensure cancellation remains terminal/sticky while leases and concurrent completion paths resolve. | Cancelled tasks do not requeue or transition back to running/completed unexpectedly. | `orchestrator/execution.py`, `repositories/sqlalchemy.py`, task execution tests | Concurrency paths are timing-sensitive and require robust coverage. | (Done) |
| T-166 | P0 | Harden interaction response state-machine behavior. | Enforce valid transitions and safe resume gating after human responses. | Interaction responses are idempotent/conflict-safe and only resume when policy allows. | `orchestrator/execution.py`, `repositories/sqlalchemy.py`, interaction tests | Clarification and permission flows can diverge without explicit transition guards. | (Done) |
| T-167 | P0 | Expand integration coverage for existing cancel/interaction endpoints. | Cover success, not-found, conflict, and idempotent paths for currently shipped endpoints; any endpoint behavior changes are tracked as separate backlog items. | Integration tests validate current endpoint behavior against real service wiring without coupling to new endpoint implementation work. | `tests/integration/test_task_endpoints.py` | Missing coverage can mask regressions in operator workflows. | (Done) |
| T-168 | P0 | Align DB migration constraints with task cancellation timeline events. | Add migration updates for `task_cancelled` timeline check constraints and validate upgrade path behavior. | Migrated databases accept `task_cancelled` timeline writes without constraint failures. | `db/migrations/versions/*`, `tests/integration/test_db_migrations.py` | Constraint drift across revisions can break production-upgraded databases. | (Done) |
| T-169 | P1 | Stabilize dashboard interaction/cancel UX behavior. | Ensure component typing, response rendering, and action states match API semantics. | Dashboard tests cover interaction response and cancel UX paths without runtime/type regressions. | `dashboard/src/components/*`, `dashboard/src/services/api.ts`, dashboard tests | UI state drift from backend semantics can confuse operators. | (Done) |
| T-170 | P1 | Add tracing/observability guardrails for native runs. | Bound trace payload size/noise while preserving actionable runtime metadata and status signals. | Native-run spans remain readable, structured, and policy-safe under heavy output. | `apps/observability.py`, `workers/native_agent_runner.py`, tracing tests | Overly noisy spans reduce debuggability and increase telemetry overhead. | (Done) |
| T-171 | P1 | Verify local e2e execution path and runbook/compose alignment. | Tighten local environment defaults, compose wiring checks, and operational runbook guidance. | Local e2e smoke path is reproducible with documented setup and verification steps. | `docker-compose.yml`, `docs/runbook.md`, infra tests/scripts | Environment skew between API/worker/dashboard/tracing can cause false negatives. | (Done) |
| T-172 | P0 | Codex native Docker sandbox-boundary alignment. | Enforce runtime policy for containerized native runs where Linux sandbox may be unavailable. | Docker boundary is explicitly managed and Codex `--sandbox danger-full-access` used only inside trusted containers. | `sandbox/policy.py`, `workers/codex_cli_worker_native.py` | Sandbox escapes if trust checks are bypassed. | (Done) |
| T-173 | P0 | Simplify native-agent prompts and enforce delivery_mode. | Refactor `workers/prompt.py` to omit junk and enforce "Review vs Fix" intent. | Workers respect delivery_mode; prompts are 30% smaller. | `workers/prompt.py`, `workers/gemini_cli_worker.py` | Over-simplification can reduce context for complex tasks. | (Done) |
| T-174 | P0 | Implement deterministic-first verification. | Refactor `orchestrator/graph.py` to run CI tests before LLM verifier. | LLM verifier is skipped on deterministic failure; timeouts reclassified. | `orchestrator/graph.py`, `orchestrator/verification.py` | Verification budget might be exceeded if CI is slow. | (Done) |
| T-175 | P0 | Add infra-failure detection to NativeAgentRunner. | Scan `stderr` for shell crash markers and return `SANDBOX_INFRA`. | Agents don't "self-heal" out-of-scope infra; orchestrator halts correctly. | `workers/native_agent_runner.py` | Pattern matching might be brittle across shell versions. | (Done) |
| T-176 | P1 | Standardize Phoenix/OpenInference tracing. | Emit structured JSON attributes and correct span kinds. | Traces are filtered by task/worker/outcome in Phoenix; JSON payloads are inspectable. | `apps/observability.py`, orchestrator nodes | Telemetry overhead might increase slightly. | (Done) |
| T-177 | P1 | Optimize discovery latency and router fallback. | Introduce "Discovery" profile and harden brain-router fallback. | Enrichment latency drops; router is resilient to brain timeouts. | `orchestrator/brain.py`, `orchestrator/graph.py` | Fallback heuristics may be less "smart" than the brain. | (Done) |
| T-178 | P1 | E2E Forensic Investigation & Runtime Hardening. | Fix 404 polling loops and harden JSON/ReviewResult parsing. | API is clean of polling spam; JSON failures are log-inspectable. | `dashboard/src/hooks/*`, `workers/self_review.py`, `orchestrator/brain.py` | Complex parsing logic can still fail; needs targeted unit tests. | (Done) |
| None | P0 | Python File Decomposition & Size Check Enforcement. | Add `scripts/check_python_file_sizes.py` and waiver tracking to enforce strict file/function sizes. | Pre-commit size checks pass; hotspots decomposed. | `scripts/check_python_file_sizes.py`, `.sizecheck-exceptions.yaml`, multiple refactors | Decomposition without regression requires careful handling. | (Done) |

Status:

- completed

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

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-186 | P0 | Define Scout Mode task type and lane parameters. | Add `'scout'` to `TaskSpecType` or runtime mode. Map a lower-priority queue lane, define separate budget defaults. | Scout tasks can be created and routed without blocking primary task lane. | `orchestrator/state.py`, `apps/api/routes/tasks.py` | Queue starvation if priorities are not strict. | (Done in #222) |
| T-187 | P0 | Add Read-Mostly sandbox policy. | Create sandbox profile that only allows reading files. Disable modifying commands except writing to a designated artifact directory. | Scout tasks fail if they attempt `git commit` or `npm install`. | `sandbox/policy.py`, `workers/native_agent_runner.py` | Over-constraining might break some code analysis tools. | (Done in #223) |
| T-188 | P0 | Implement Idea Inbox / Proposal store. | Add `Proposal` DB model tied to `Session`. Allow tasks to emit ideas instead of final code. | Ideas are durably stored with origin metadata and status (`PENDING_REVIEW`). | `db/models.py`, `repositories/sqlalchemy.py` | Schema migration needs care. | (Done in #224) |
| T-189 | P0 | Route Scout output to Review Inbox. | Ensure Scout tasks do not merge or deploy. Their artifacts transition to a `PENDING_REVIEW` proposal state. | Scout outputs only show up in the Idea Inbox and never execute mutations on main codebase. | `orchestrator/execution.py`, `orchestrator/graph.py` | Escape via tool loop if boundaries are weak. | (Done in #226) |
| T-190 | P1 | Dashboard UI for Idea Inbox. | Surface Scout proposals in the dashboard. Operator can review, reject, or promote them to real tasks. | Operator can click "Accept Idea" to turn it into a queued execution task. | `dashboard/src/components/*`, `dashboard/src/services/api.ts` | State drift between UI and backend. | (Done in #227) |
| T-191 | P2 | Add Trigger Sources: Schedule and Idle time. | Create a cron-like scheduler or API endpoints that spawn Scout tasks based on configured intervals or system idleness. | Background task generation works without human input. | `apps/api/scheduler.py` | Spawning loops could consume budget quickly. | (Done) |
| T-200 | P0 | Skip change-oriented review for read-only tasks. | Change read-only execution policy so worker self-review and independent code-change review are skipped when `read_only=true` or no files changed; keep deterministic verification only when explicit safe commands exist. | Read-only Scout/investigation tasks no longer run diff-oriented review, while mutable tasks still do. | `workers/self_review.py`, `orchestrator/review.py`, `orchestrator/nodes/verification.py` | Must preserve useful verification for read-only investigations without running change-review prompts. | Planned |
| T-201 | P1 | Add structured Scout trigger parameters and repo allowlist. | Extend Scout trigger request support for `mode`, `repo_key`, optional `branch`, `focus`, `depth`, and capped `max_proposals`; resolve `repo_key` through configured allowlisted repos instead of arbitrary repo URLs. | Default no-body trigger still works; configured repo keys resolve safely; research mode rejects missing focus; invalid/capped params return clear validation errors. | `apps/api/routes/tasks.py`, `apps/api/config.py`, dashboard trigger UI/tests | Input flexibility could expand remote-code intake; keep repo selection allowlisted and server-controlled. | Planned |
| T-202 | P1 | Add task-type prompt overlays for Scout modes. | Add prompt overlays on top of the shared worker prompt for `repo_scout`, `research_scout`, and `deep_scout`, with proposal-oriented output rules, evidence requirements, and read-only guardrails. | Scout prompts are mode-specific, generic workers keep the shared base prompt, and tests assert the expected overlay content. | `workers/prompt.py`, worker prompt tests | Prompt drift can make task types inconsistent; keep overlays small and composable. | Planned |
| T-203 | P1 | Implement Repo Scout and Research Scout modes. | Route `mode=repo` to read-only repository inspection and `mode=research` to topic-driven research proposal generation with explicit focus/topic input. | Repo Scout produces repo-evidenced proposals; Research Scout produces source-aware proposals; both land in Idea Inbox with mode metadata. | `orchestrator/task_spec.py`, `orchestrator/execution_outcome_service.py`, Scout API/dashboard tests | Research Scout needs explicit network/source policy to avoid noisy or stale recommendations. | Planned |
| T-204 | P2 | Implement Deep Scout chaining. | Add explicit `mode=deep` / `repo_then_research` flow that chains repo inspection into targeted research with a higher but still capped budget. | Deep Scout runs only when explicitly requested, records chain metadata, and produces a richer Idea Inbox proposal without mutating code. | `orchestrator/graph.py`, `orchestrator/execution_outcome_service.py`, Scout integration tests | Chaining can consume budget quickly; require explicit operator intent and strict caps. | Planned |

## Milestone 19: Reflection and Improvement Pipeline

Goal:

- convert execution friction into structured, reviewable improvement proposals

Planned deliverables:

- friction report schema
- improvement suggestion schema
- proposal scoring/planning by value, effort, risk, layer impact, validation path, and HITL need
- review queue for improvement proposals

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-192 | P0 | Define Reflection and Improvement schemas. | Add Pydantic models for `FrictionReport` and `ImprovementSuggestion` (including scoring fields: value, effort, risk, layer_impact, validation_path, hitl_need). | Schemas validate successfully and have robust type definitions. | `orchestrator/reflection.py` (new), `tests/unit/test_reflection_schemas.py` | Schema definitions might drift without strict Pydantic enforcement. | (Done) |
| T-193 | P0 | Integrate schemas with DB Proposal model. | Add a `proposal_type` column/enum to distinguish reflection improvements from scout ideas. Extend JSON payload mapping. | DB migration applies cleanly; old `Proposal` rows default to `'scout'`. | `db/models.py`, `db/enums.py`, `repositories/sqlalchemy.py` | DB migration schema evolution needs care. | Completed |
| T-194 | P1 | Capture execution friction from worker runtime. | Modify the worker failure/verifier rejection paths to automatically emit a `FrictionReport` capturing the context, source, and impact. | Friction reports are generated automatically on repeated command/test failures. | `workers/native_agent_runner.py`, `orchestrator/verification.py` | Too much noise if we report on every single small failure. | Completed |
| T-195 | P1 | Generate Scored Improvement Proposals. | Use deterministic synthesis to analyze `FrictionReport`s and produce scored `ImprovementSuggestion`s with effort/risk scoring. | Emits actionable proposals stored as `PENDING_REVIEW` in the DB. | `orchestrator/improvement_suggestions.py`, `orchestrator/execution_outcome_service.py` | Scoring may require future LLM enrichment, but the first slice avoids model latency. | Completed |
| T-196 | P1 | Add LLM-Based Improvement Proposal Scoring. | Use an optional model-backed scorer to generate or revise `ImprovementSuggestion` scoring fields and rationale from `FrictionReport` evidence; keep deterministic scoring as fallback. | Feature flag controls LLM scoring; failed/time-out model calls fall back to deterministic suggestions; metadata records model rationale and fallback status; tests cover success, fallback, and disabled-flag behavior. | `orchestrator/improvement_suggestions.py`, `orchestrator/brain.py`, `orchestrator/execution_outcome_service.py` | Model latency and nondeterminism can make proposal quality inconsistent. | Completed |
| T-197 | P1 | Dashboard UI for Reflection & Improvement Queue. | Extend the Idea Inbox UI to also display Friction Reports and Improvement Suggestions with their new scoring fields. | Operators can view, approve, or reject structural improvements. | `dashboard/src/components/*`, `dashboard/src/services/api.ts` | UI clutter if too many proposals are generated. | Completed |
| T-198 | P1 | Add dashboard trigger tab for task and scout actions. | Add a dedicated dashboard view for operator-triggered actions using existing APIs, including generic task submission shortcuts and `/tasks/scout/trigger`; do not add a manual reflection trigger in this slice because reflection remains outcome-driven. | Operator can trigger configured Scout runs and submit task-style trigger actions from the dashboard; UI shows clear success/error/loading states and does not expose secrets. | `dashboard/src/components/*`, `dashboard/src/services/api.ts`, dashboard tests | Trigger controls could accidentally encourage budget-heavy runs; keep controls explicit and scoped to existing authenticated APIs. | Completed |
| T-199 | P1 | Full dashboard QA, visual polish, limits, and reusable QA skill. | Audit the full dashboard in browser across core routes; fix high-confidence bugs, interaction rough edges, visual overflow, empty/error/loading states, and obvious performance issues; document the repeatable workflow as a repo-local dashboard QA skill. | Dashboard QA produces verified fixes, browser evidence, coverage remains above threshold, and `.agents/skills/` contains a reusable QA workflow for future dashboard passes. | `dashboard/src/*`, `.agents/skills/*`, dashboard tests | Scope can sprawl; prioritize bugs, usability regressions, visual limits, and reusable verification over broad redesign. | Planned |

Manual-only zones:

- auth/security
- secrets/sandbox boundaries
- approval core logic
- deployment/billing controls

## Milestone 19.5: Gemini to Antigravity Migration

Goal:

- make Antigravity CLI the canonical public worker identity and migration target for the current Gemini CLI worker lane

Planned deliverables:

- canonical `antigravity` worker type and profile names
- Antigravity native CLI adapter using `agy -p` / `agy --print`
- prompt-as-argv support in the native runner for CLIs that require it
- Docker worker support using official Antigravity install/auth mechanisms
- updated e2e QA, runbook, compose, env, dashboard/API labels, and operator guidance
- removal of Gemini CLI defaults after Antigravity Docker e2e is proven

Public interface direction:

- canonical worker type becomes `antigravity`; `gemini` is not the long-term public name
- canonical profiles become `antigravity-native-executor`, `antigravity-native-executor-read-only`, `antigravity-native-planner`, `antigravity-native-reviewer`, and `antigravity-native-discovery`
- Antigravity env vars are `CODE_AGENT_ANTIGRAVITY_CLI_BIN`, `CODE_AGENT_ANTIGRAVITY_MODEL`, `CODE_AGENT_ANTIGRAVITY_TIMEOUT_SECONDS`, `CODE_AGENT_ANTIGRAVITY_AUTH_DIR`, `CODE_AGENT_ANTIGRAVITY_NATIVE_SANDBOX_ENABLED`, `CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION`, and `CODE_AGENT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY`
- temporary `gemini` compatibility aliases are allowed only as a migration bridge, with warnings and tests

Manual-derived constraints:

- `agy` one-shot automation uses prompt-as-argv (`agy -p "<prompt>"`, also exposed locally as `--print`); command logging must redact prompt text because it is no longer stdin-only
- Antigravity stores preferences and permissions in `~/.gemini/antigravity-cli/settings.json`, including `toolPermission`, `artifactReviewPolicy`, and `enableTerminalSandbox`
- supported permission modes include `request-review`, `proceed-in-sandbox`, `always-proceed`, and `strict`; the platform must map worker profiles to these modes explicitly instead of relying on interactive prompts
- Antigravity auth uses the operating-system secure keyring (Apple Keychain, Linux Secret Service over DBus, or Windows Credential Manager), so `CODE_AGENT_ANTIGRAVITY_AUTH_DIR` must not imply host keychain copying or secret scraping
- Antigravity parses workspace `GEMINI.md` and `AGENTS.md`; migration docs must also cover legacy plugin import, skills paths, and MCP config movement into `.agents/`
- desktop app installation can share settings with the CLI, but it does not by itself prove auth is available inside a Linux Docker worker

Reference manuals:

- [Antigravity CLI overview](https://antigravity.google/docs/cli-overview)
- [Antigravity CLI reference](https://antigravity.google/docs/cli-reference)
- [Antigravity CLI install](https://antigravity.google/docs/cli-install)
- [Antigravity CLI getting started](https://antigravity.google/docs/cli-getting-started)
- [Antigravity CLI troubleshooting](https://antigravity.google/docs/cli-troubleshooting)
- [Antigravity CLI best practices](https://antigravity.google/docs/cli-best-practices)
- [Antigravity CLI sandbox](https://antigravity.google/docs/cli-sandbox)
- [Antigravity CLI permissions](https://antigravity.google/docs/cli-permissions)
- [Gemini CLI to Antigravity migration](https://antigravity.google/docs/gcli-migration)

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-205 | P0 | Rename canonical worker identity from Gemini to Antigravity. | Add `antigravity` worker type, profile names, API/dashboard labels, and DB enum/check-constraint migration from persisted `gemini` rows to `antigravity`. Keep temporary `gemini` aliases only where needed for migration. | Existing `gemini` task/run rows upgrade to `antigravity`; new submissions accept `antigravity`; deprecated `gemini` inputs warn or map predictably during the bridge period. | `db/enums.py`, `workers/base.py`, `db/migrations/versions/*`, routing/API tests | Broad type/name churn can break replay, snapshots, and dashboards if aliases are inconsistent. | Not Started |
| T-206 | P0 | Add Antigravity native CLI adapter. | Build an adapter around `agy -p` / `agy --print <prompt> --print-timeout ... --model ... --log-file ...`; generate or mount the per-run Antigravity settings needed for `toolPermission`, `artifactReviewPolicy`, and `enableTerminalSandbox`; map stdout, JSON responses, provider/auth errors, timeout, diff, and changed files into `WorkerResult`. | Fake `agy` tests cover success, JSON output, timeout, auth/provider failure, permission prompt/denial, no-change success, settings generation, and changed-file collection. | `workers/*antigravity*`, `apps/api/task_service_factory.py`, worker tests | `agy -p` / `--print` consumes the prompt as an argument and differs from Gemini CLI flags, permissions, and auth behavior; it must not be treated as a drop-in binary rename. | Not Started |
| T-207 | P0 | Support prompt-as-argv in native runner. | Extend native-agent execution so adapters can place the prompt in argv while existing CLIs keep stdin prompt delivery. Redact prompt text from logged/sanitized command strings. | Native runner tests prove stdin remains default, `agy` prompt-in-argv works, command logs do not expose full prompt/secrets, and timeouts still collect artifacts. | `workers/native_agent_runner.py`, `workers/native_agent_models.py`, runner tests | Long prompts can hit argv limits; adapter must fail clearly or use a bounded strategy. | Not Started |
| T-208 | P0 | Add Docker Antigravity support. | Install Antigravity CLI in the worker image using the official CLI install path and prove auth works inside compose through official Antigravity keyring mechanisms. For Linux containers, validate Secret Service/DBus requirements or document the official blocker. Do not invent keychain bypasses or scrape host secrets. | Docker smoke passes for `agy models` and `agy --print 'Reply with OK only'`; permission settings are deterministic for non-interactive runs; if official non-interactive auth is unavailable, the blocker is documented and the milestone remains incomplete. | `Dockerfile.worker`, `docker-compose.yml`, `.env.example`, Docker/e2e scripts | Antigravity desktop/keychain auth may not transfer safely into Linux containers, and headless DBus/keyring support may require an official container-friendly auth path. | Not Started |
| T-209 | P1 | Update e2e QA and operator docs. | Replace Gemini defaults and auth guidance with Antigravity guidance in e2e scripts, README, runbook, compose docs, and env examples. Include `agy` install/PATH guidance, keyring/DBus troubleshooting, permission presets, `AGENTS.md`/`GEMINI.md` context behavior, legacy plugin import, skills path migration, and MCP config relocation. | Local e2e instructions use `worker_override=antigravity`; stale `gemini auth login` guidance is removed or marked legacy; operator docs explain how to diagnose `agy: command not found`, locked keyrings, and permission-prompt timeouts. | `.agents/skills/e2e-qa/scripts/*`, `README.md`, `docs/runbook.md`, `.env.example` | Docs can drift if code-level aliases remain during the migration bridge. | Not Started |
| T-210 | P1 | Update dashboard/API worker labels. | Update operator-visible worker/profile labels and frontend/API contract fixtures to show `antigravity` names. | Dashboard and API tests cover `worker_override=antigravity`, Antigravity profiles, and legacy alias display behavior. | `dashboard/src/*`, `apps/api/routes/*`, API/dashboard tests | UI/API compatibility must be explicit for existing saved tasks and replays. | Not Started |
| T-211 | P1 | Remove Gemini CLI defaults after Antigravity e2e passes. | Drop Gemini CLI from the default worker image/config and leave only documented temporary aliases if still needed. | Default compose image uses Antigravity, full webhook e2e passes with `antigravity`, and Gemini CLI is no longer required for local happy path. | `Dockerfile.worker`, `docker-compose.yml`, docs/tests | Removing Gemini too early can break enterprise/API-key users before the migration bridge is verified. | Not Started |

Testing and acceptance:

- unit tests cover worker enum/profile validation, service factory wiring, Antigravity command construction, env allowlist, and native-runner prompt delivery mode
- migration integration tests prove existing `gemini` task/run rows upgrade to `antigravity` and constraints accept the new canonical value set
- fake `agy` tests cover success, JSON output, timeout, auth/provider failure, permission prompt/denial, settings generation, and changed-file collection
- Docker smoke covers `agy models` and `agy --print 'Reply with OK only'` inside the worker container
- full webhook e2e passes with `worker_override=antigravity` after Docker auth is proven
- dashboard/API contract tests cover worker override and profile names

Assumptions:

- Milestone 19 remains active; Milestone 19.5 is the migration bridge before Phase 3
- existing Milestones 20 and 21 keep their numbers
- Docker support is required for the milestone to complete, but auth must use official Antigravity mechanisms only
- if official Antigravity CLI cannot authenticate non-interactively in Docker, T-208 documents the blocker and the milestone remains incomplete rather than shipping an unsafe workaround

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
3. Milestone 19.5

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

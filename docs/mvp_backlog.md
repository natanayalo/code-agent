# MVP Backlog

## Milestone 0 - Bootstrap

### T-001 Initialize project skeleton
Create Python project structure, tooling, and base package layout.

Canonical task spec:
- `docs/tasks/t_001_initialize_project_skeleton.md`

Supporting reference:
- `docs/bootstrap_file_list.md`

Acceptance:
- repo structure exists
- formatter/linter/test tooling configured
- app starts locally

### T-002 Add local infrastructure
Add docker-compose for Postgres and API service.

Acceptance:
- local stack boots with one command
- DB is reachable from app

### T-003 Add health endpoints
Implement `/health` and `/ready`.

Acceptance:
- both endpoints return success in local dev

---

## Milestone 1 - Persistence + state

### T-010 Add DB models
Create tables for users, sessions, tasks, worker_runs, artifacts, memory_personal, memory_project.

Acceptance:
- migrations apply
- models usable in app

### T-011 Add repository layer
Implement DB repositories for sessions, tasks, runs, and memory.

Acceptance:
- CRUD works in tests

### T-012 Define orchestrator state schema
Create typed state model for the workflow.

Acceptance:
- state model validated in tests

### T-013 Normalize persistence enums and constrained value fields
Define canonical enums for persisted statuses and artifact types, then align the ORM schema and migrations.

Acceptance:
- status and artifact vocabularies are explicitly defined
- ORM models use typed enums or equivalent constrained values
- migrations preserve compatibility with existing persisted rows

---

## Milestone 2 - Orchestrator skeleton

### T-020 Build LangGraph workflow skeleton
Implement nodes:
- ingest_task
- classify_task
- load_memory
- choose_worker
- dispatch_job
- await_result
- summarize_result
- persist_memory

Acceptance:
- graph runs end-to-end with fake worker

### T-021 Add checkpoint persistence
Persist workflow state between restarts.

Acceptance:
- interrupted run can resume

### T-022 Add approval interrupt node
Support a pause/resume step for destructive actions.

Acceptance:
- task can pause and resume cleanly

---

## Milestone 3 - Sandbox

### T-030 Create workspace manager
Implement task workspace creation, repo clone, cleanup policy.

Acceptance:
- workspace created per task
- repo available inside workspace

### T-031 Add Docker sandbox runner
Run commands in isolated container with mounted workspace.

Acceptance:
- sample command runs in sandbox
- stdout/stderr captured

### T-032 Add artifact capture
Persist command logs, changed-file list, and optional diff summaries.

Acceptance:
- artifacts visible after run

---

## Milestone 4 - First worker

### T-040 Define worker interface
Create shared task/result models and abstract worker interface.

Acceptance:
- orchestrator can call fake worker through interface

### T-041 Implement CodexWorker or GeminiWorker
Pick one first and integrate with real task execution through the shared async worker contract.

Acceptance:
- real worker executes one toy repo task through the shared async worker interface
- orchestrator awaits worker execution through the shared worker contract
- worker returns a contract-compliant result to the orchestrator

Architecture review checkpoint (non-milestone, after T-041):
- verify real worker outputs conform to the worker interface contract
- verify orchestrator state schema survives real execution paths
- document any interface/state mismatches before continuing

---

## Milestone 5 - Vertical Slice E2E

### T-045 Evolve sandbox to persistent container with shell sessions
Add a long-lived container mode so the worker can send multiple commands iteratively
(think → act → observe → loop) instead of one-shot `docker run` invocations.

Scope notes:
- add `sandbox/container.py` for container lifecycle: start, stop, reconnect to a named Docker container
- add `sandbox/session.py` for shell sessions: maintain a single long-lived interactive shell process inside the container (via `docker exec -it` or equivalent piped I/O) so that environment variables, `cd`, and `export` persist across commands between agent turns; do not use a fresh `docker exec` per command
- move or expose `_read_stream_bounded` (currently private in `sandbox/runner.py`) to a shared utility within the `sandbox` package before `session.py` depends on it
- keep `DockerSandboxRunner` as-is (used by toy CodexWorker and existing tests)

Acceptance:
- named container can be started, used for multiple `docker exec` commands, and stopped
- commands share filesystem state (file created in command 1 is visible in command 2)
- output capture uses the same bounded-reader safety as the existing runner
- container cleanup is reliable (stop + remove on session end or error)

### T-046 Build structured system prompt module
Create the prompt construction layer that assembles a system prompt from modular
sections, following the Open-SWE pattern.

Scope notes:
- add `workers/prompt.py` with `build_system_prompt()` that assembles: role description, available tools, repo-level context (AGENTS.md + directory listing), task-specific context, and workflow instructions
- each prompt section is a separate function for testability
- reads AGENTS.md from workspace if present, gracefully skips if absent
- separate stable session scaffold content from dynamic task/run context so CLI adapters can persist or reuse what they control externally without depending on undocumented cache behavior
- workflow instructions must explicitly teach failure recovery and bounded-output habits (for example: verify assumptions after a failed command, and prefer focused reads over dumping large files)

Acceptance:
- `build_system_prompt()` produces a well-structured prompt given a `WorkerRequest` and workspace path
- AGENTS.md injection and repo structure listing both work
- workflow instructions explicitly cover command-failure recovery and focused/bounded output usage
- unit tests verify prompt assembly for various input combinations

### T-047 Implement CLI-driven multi-turn worker runtime
Replace the one-shot toy-script pattern with an iterative agent loop: prompt →
runtime adapter → tool call → execute via persistent shell → feed observation back → loop.

Scope notes:
- add a shared CLI runtime layer under `workers/` plus provider-specific adapters such as `GeminiCliWorker` and `CodexCliWorker`
- build system prompt via T-046's `build_system_prompt()`
- start persistent container via T-045's container/session layer
- start with a single `execute_bash` tool (per mini-SWE-agent's proven bash-only approach)
- format shell observations before feeding them back to the LLM: include exit code, bounded output, and explicit truncation markers instead of raw unbounded stdout/stderr dumps
- the worker contract must stay useful whether the underlying runtime is a CLI, SDK, hook-based wrapper, or direct API
- do not assume full ownership of low-level raw API payload assembly unless a specific runtime adapter actually provides it
- exit on: LLM final answer, max iterations, or budget/timeout exceeded
- the loop is a simple while-loop, not a nested LangGraph graph
- the worker must include its own inner-loop safety envelope (max iterations, worker-local timeout, and budget checks) so it never runs unbounded; T-042 adds the outer orchestrator-level timeout/cancel layer on top of this, but T-047 must be safe to run standalone

Acceptance:
- worker executes a real multi-step task (e.g., create a file, run a test, fix a failure)
- agent loop runs ≥2 iterations, demonstrating observe-then-act behavior
- observation framing keeps shell feedback bounded and includes exit codes/truncation markers
- the worker path does not require direct API-only tool-calling assumptions
- timeout/budget limits terminate the loop safely
- worker returns a clean `WorkerResult` (not an exception) when the budget or timeout is exceeded
- `WorkerResult` accurately reflects commands run, files changed, and final status

### T-048 Add explicit tool registry and bash tool boundary
Define the initial tool layer as a small, policy-aware registry rather than ad hoc tool strings embedded only in prompts.

Scope notes:
- add a tool registry under `tools/` with metadata such as: `name`, `capability_category`, `side_effect_level`, `required_permission`, `timeout`, `network_required`, `expected_artifacts`, and `deterministic`
- start with one tool: `execute_bash`
- feed the registry into prompt construction, runtime enforcement, and future MCP compatibility work
- keep the interface small and typed; do not build a generalized framework before the first real worker path exists

Acceptance:
- the initial tool surface is declared in one explicit registry
- the worker runtime can look up permission, timeout, and artifact expectations from the registry
- prompt construction reflects the same registry metadata used by execution

### T-049 Add permission ladder and runtime budget ledger
Replace the coarse single approval gate with explicit permission classes and real execution budgeting.

Scope notes:
- add permission classes such as: `read_only`, `workspace_write`, `dangerous_shell`, `networked_write`, and `git_push_or_deploy`
- map each tool or command path to a permission class through the tool registry/policy layer
- approval should escalate only when the requested action exceeds the currently granted permission level
- implement a runtime budget ledger for wall-clock time, worker iterations, tool-call count, shell-command count, retry count, and verifier passes
- token/cost accounting is best-effort: record it when the CLI/runtime exposes usage, but keep the system functional when it does not

Acceptance:
- approval enforcement happens at tool/command execution time, not only from task-text heuristics
- the worker and orchestrator can pause/resume cleanly when a higher permission level is required
- runtime budgets terminate over-budget work safely with a structured result rather than an unbounded loop
- CLI-based runs still enforce budgets even when token usage is unavailable

### T-042 Add baseline worker timeout/cancel handling
Add the outer orchestrator-level timeout and cancellation behavior required so the first real worker execution path cannot hang indefinitely even if the worker's own inner-loop guards are exhausted or fail to stop progress.

Acceptance:
- hung worker or sandbox execution fails safely within a configured timeout enforced by the orchestrator path
- timeout/cancel failure is surfaced back to the orchestrator without blocking the run forever
- workspace/logs are preserved for debugging after timeout
- any partial permission/budget/verifier state needed for diagnosis is preserved alongside the workspace artifacts

### T-044 Run one real orchestrator-to-worker vertical slice
Execute one real task submitted via curl, routed through orchestrator, run by the multi-turn agent worker in a real workspace, with results persisted and returned.

Scope notes:
- builds on persistent sandbox (T-045), system prompt (T-046), CLI worker runtime (T-047), tool registry (T-048), permission/budget enforcement (T-049), and outer timeout/cancel handling (T-042)
- uses the multi-turn CLI worker path, not the toy CodexWorker
- includes the minimal HTTP task submission endpoint needed for curl-based validation
- includes a basic task status/result retrieval endpoint (GET by task_id) so callers can poll for completion
- includes execution-path persistence wiring for task/status, final result fields, worker run metadata, and captured artifacts needed for submission + GET-by-task_id polling
- worker should complete at least one multi-step coding task end-to-end
- Telegram is explicitly out of scope for this milestone
- hardcoded repo URL and task text are acceptable
- no mocks or fake worker results
- structured memory repository wiring remains out of scope here; `load_memory` / `persist_memory` stay stubbed until T-060..T-065
- result *delivery* (push-based, e.g. Telegram reply or webhook callback) is out of scope here and covered by the Telegram ingress milestone (T-050..T-053)

Acceptance:
- `curl` submission reaches the minimal HTTP task endpoint and starts a real run
- orchestrator routes to the implemented real worker
- worker executes a multi-step task in a real workspace via the agent loop and returns real output
- final result and run artifacts are persisted to DB
- API returns a task identifier and initial status for the asynchronous run
- completed result is retrievable via GET endpoint by task_id

---

## Milestone 6 - Sandbox hardening

### T-054 Harden sandbox execution boundary and auditability
Harden execution so one real command runs only inside the sandbox with complete audit artifacts and replay-friendly metadata.

Acceptance:
- one real command is executed in sandbox (not host process execution)
- stdout/stderr, changed files, and diff-summary artifacts are captured and persisted
- file allow/deny policy is explicit for worker-visible paths
- secret-bearing output is redacted before durable storage where feasible
- command replay metadata and risk-scoring inputs are persisted
- milestone is only done when all checks pass without bypass flags

### T-055 Add constrained verifier stage
Add a verifier after the builder worker completes, starting with a deterministic verifier rather than a second unconstrained agent.

Scope notes:
- add a `verify_result` node after `await_result` and before `summarize_result`
- verifier inputs should include: task request, worker result, changed-file list, diff summary, command audit metadata, test results, and workspace artifacts
- verifier checks should cover: task/output match, intended file scope, test pass/fail state, risky command usage, suspiciously large diffs, and obvious regressions
- the verifier should emit a structured verification report rather than free-form text only
- keep verification constrained and explainable before considering any agentic verifier follow-up

Acceptance:
- the orchestrator graph includes a verifier stage
- verifier output is structured and persisted with the run
- obvious mis-scoped diffs, risky commands, and failed tests are surfaced before final summarization
- verifier can fail or request follow-up without requiring a second unconstrained builder agent

---

## Milestone 7 - Memory integration

### T-060 Add skeptical memory schema and metadata
Store memory as structured hints with explicit provenance and verification metadata.

Acceptance:
- memory entries store fields such as `source`, `confidence`, `scope`, `last_verified_at`, and `requires_verification`
- existing personal/project memory repositories can create/read/update the richer schema

### T-061 Add compact session working state store
Store concise working state for long-running sessions instead of relying on transcript-shaped context.

Acceptance:
- compact session state preserves: current goal, decisions made, files touched, unresolved risks, and user/project preferences
- compact state can be created/read/updated independently from transcript history
- compact state update semantics are explicit and tested, including whether nested JSON fields use shallow merge or deep merge behavior
- compact state callers can intentionally distinguish between omitting a field and explicitly clearing it where that behavior is supported

### T-062 Add skeptical memory retrieval and verification policy
Load relevant memory before routing/dispatch while treating stored claims as hints rather than truth.

Acceptance:
- second run on the same repo sees prior memory context and compact state
- filesystem and repo-structure claims from memory are verified before action where applicable

### T-063 Add memory admin endpoints
Inspect and edit memory entries manually.

Acceptance:
- memory metadata and compact session state can be listed and modified

### T-064 Wire load_memory → execute → persist_learnings in orchestrator
Run the full memory loop on a real task execution path with skeptical retrieval and structured compaction.

Acceptance:
- orchestrator loads structured memory before dispatch on a real task
- worker/orchestrator integration carries structured session-state fields for decisions made and identified risks instead of relying on heuristic parsing of free-form summaries
- execution persists verified learnings back to memory stores
- updated compact session working state is persisted after each real run
- stored memory is inspectable and retrievable via repositories/endpoints
- no opaque blob-only memory payloads are introduced

### T-065 Add stable session scaffold persistence
Persist the stable session scaffold separately from dynamic turn state so CLI-based workers can preserve reusable context where possible.

Acceptance:
- durable instructions, tool manifest, session policy, stable project context, and compact memory header are persisted separately from dynamic task observations
- if a CLI/runtime exposes resumable session handles, they are stored and restored
- if a CLI/runtime does not expose resumable handles, the system still reconstructs the same stable scaffold deterministically without relying on undocumented cache behavior

---

## Milestone 8 - Structured run observability

### T-043 Add structured run logs
Expand worker run metadata and output summaries beyond the baseline persistence required by T-044.

Acceptance:
- task run records include session_id, task_id, chosen worker, route reason, workspace id, start/end timestamps, final status, changed files count, and artifact list
- sandbox command records include command, exit code, duration, and stdout/stderr artifact locations
- permission escalations, budget usage, and verifier outcomes are queryable in persisted run data
- structured run summaries are queryable in DB/logs without relying only on free-form text blobs
- `worker_runs` persists structured observability fields for `session_id`, `requested_permission`, `budget_usage`, and `verifier_outcome`
- persisted command records can carry artifact references for captured stdout/stderr logs when those artifacts exist
- task snapshot/read APIs expose the latest-run structured observability fields needed for inspection without requiring direct DB reads
- tests cover both persistence and retrieval of the new observability fields

---

## Milestone 9 - Second worker routing

### T-070 Implement second worker adapter ✅
Add remaining worker so both Gemini and Codex are supported.

Acceptance:
- both workers runnable via the same orchestrator path and shared CLI-runtime abstractions

### T-071 Add routing heuristics
Implement route policy and route-reason logging.

Acceptance:
- route decision stored with task
- routing can consider runtime availability, budget preference, task shape, and prior verifier failure modes

### T-072 Add manual worker override
Allow caller to pin a worker for a task.

Acceptance:
- override bypasses default routing when runtime availability and policy still allow the selected worker

---

## Milestone 10 - Telegram ingress (minimal real flow)

### T-050 Add generic webhook adapter
Accept JSON webhook payloads and translate them onto the existing HTTP task submission path.

Acceptance:
- posting a webhook payload creates a task through the existing HTTP path

### T-051 Add Telegram webhook adapter
Receive Telegram messages and map them to sessions.

Acceptance:
- Telegram message triggers workflow

### T-052 Add progress replies
Send progress updates back to Telegram/webhook callback.

Acceptance:
- user sees at least start / running / done or failed

### T-053 Add dedupe protection
Prevent duplicate webhook deliveries from creating duplicate tasks.

Acceptance:
- repeated delivery is idempotent

Acceptance for milestone completion:
- one `/task` command from Telegram is accepted by the bot
- Telegram task is submitted to the existing HTTP task path
- chat receives at least one progress update and one final result from the real flow

---

## Milestone 11 - Tools

### T-080 Add git utility wrapper
Expose git status, diff, branch, commit helpers.

Acceptance:
- worker/orchestrator can use git helper consistently through the shared tool registry introduced earlier

### T-081 Add GitHub wrapper
Expose PR/comment/status helpers behind internal tool layer.

Acceptance:
- can create draft PR metadata or comments in tests

### T-082 Add browser/search wrapper
Add minimal fetch/search capability.

Acceptance:
- wrapper returns normalized results

### T-083 Add MCP client abstraction
Add tool boundary ready for MCP migration.

Acceptance:
- at least one tool accessible through abstraction without bypassing the internal policy-aware tool registry

### T-084 Add lifespan-managed shared HTTP clients for outbound notifier adapters
Introduce app-owned async HTTP clients for outbound integrations such as Telegram progress delivery and webhook callbacks.

Acceptance:
- outbound notifier adapters can reuse shared HTTP clients without per-notification client construction
- client lifecycle is explicitly owned by app startup/shutdown or equivalent test-safe hooks
- tests verify clients are closed cleanly

### T-085 Isolate parallel progress notifier delivery
Dispatch progress notifications to multiple backends without allowing one slow backend to delay the others.

Acceptance:
- notifier fan-out runs in parallel
- per-backend failures are logged with backend identity and do not suppress sibling deliveries
- per-backend timeouts are enforced so stuck callbacks do not block task completion

### T-086 Harden outbound callback SSRF defenses beyond literal-IP validation
Strengthen progress webhook callback delivery against DNS rebinding and hostname-based private-network targeting.

Acceptance:
- outbound callback delivery validates resolved destination addresses, not just literal IP hosts
- private, loopback, link-local, reserved, multicast, and unspecified targets are blocked even when reached through hostnames
- the chosen mitigation is documented clearly, whether it is transport-level IP pinning, egress policy enforcement, or both

### T-087 Harden outbound callback delivery transport
Move callback protection from submission-time screening alone toward transport-time enforcement.

Acceptance:
- callback delivery validates the connected remote IP immediately before or during connect, not only at submission time
- the implementation closes the DNS rebinding gap via IP pinning, transport-level validation, or equivalent egress control
- hostname resolution work used for callback safety has an explicit bounded strategy rather than relying only on unbounded default resolver behavior

### T-088 Read .agents/ skills and workflows from target workspace
Extend the system prompt module to discover and inject `.agents/` assets (skills, workflows, rules) from the target workspace alongside the existing `AGENTS.md` injection.

Scope notes:
- extend `workers/prompt.py` to scan `.agents/skills/`, `.agents/workflows/`, and `.agents/rules/` from the workspace root
- inject discovered asset summaries (name + description or first lines) into the system prompt so the worker is aware of repo-specific coding patterns
- respect the existing bounded-read safety (`DEFAULT_AGENTS_MAX_CHARACTERS`) — total injected guidance from `AGENTS.md` + `.agents/` must stay within budget
- gracefully skip when `.agents/` is absent or empty (most repos won't have it)
- do not require `.agents/` to follow any fixed schema beyond readable markdown files
- a built-in default skill set for repos with no guidance is out of scope for this task

Acceptance:
- worker system prompt includes relevant `.agents/` content when present in the target workspace
- total injected repo guidance respects the existing character budget
- repos without `.agents/` work exactly as before
- unit tests verify discovery, injection, and budget enforcement

### T-089 Add structured file editing tools
Add dedicated `view_file`, `str_replace_editor`, `search_file`, and `search_dir` tools alongside the existing `execute_bash` tool so the worker can read and edit files without constructing fragile shell one-liners.

Scope notes:
- implement tools as Python functions executed inside the sandbox shell session (not host-side)
- `view_file`: read a file with optional line-range windowing; include line numbers in output
- `str_replace_editor`: replace an exact string occurrence in a file; fail clearly on ambiguous or missing match
- `search_file`: regex/literal search within a single file, returning matching lines with context
- `search_dir`: regex/literal search across a directory tree, returning file paths and matching lines with context
- register all tools in the tool registry with appropriate permission levels (`workspace_write` for editor, `read_only` for the rest)
- feed tool definitions into prompt construction via the existing registry path
- keep tool implementations small; delegate to shell commands where practical (e.g., `grep -rn --exclude-dir=.git --exclude-dir=__pycache__` for search_dir)
- the worker runtime adapter must dispatch these tools the same way it dispatches `execute_bash`

Acceptance:
- worker can view, search, and edit files through dedicated tools without relying solely on bash
- tool registry includes all new tools with correct permission metadata
- system prompt reflects the expanded tool surface
- str_replace_editor rejects ambiguous matches and reports clear errors
- unit tests cover each tool's happy path and error cases

### T-107 Inject repo CI/build config into worker context
Extend the system prompt to extract and inject key build/test/lint configuration from the target workspace so the worker knows how to build and verify changes.

Scope notes:
- scan for common config files: `Makefile`, `package.json` (scripts section), `pyproject.toml` (tool/scripts/test sections), `.github/workflows/` (CI job names and trigger events), `CONTRIBUTING.md`, `Dockerfile`
- extract only the actionable sections (e.g., `scripts` from `package.json`, `[tool.pytest]` from `pyproject.toml`) — do not dump entire files
- inject a concise "Build & Test" section into the system prompt between repo context and task context
- respect the existing character budget for total repo context
- gracefully skip when none of these files exist

Acceptance:
- worker system prompt includes build/test/lint commands when discoverable from the workspace
- extraction is bounded and does not blow up the prompt on large config files
- repos with no recognizable build config work exactly as before
- unit tests verify extraction for Python and Node.js project layouts

---

## T-104 — API authentication (pulled ahead of Milestone 12)

### T-104 Add API authentication
Add shared-secret or signature-based authentication for the HTTP task and webhook endpoints.

Scope notes:
- protect both `/tasks` and `/webhook` with a consistent auth mechanism
- support at minimum a shared secret via header (e.g., `X-Webhook-Token`) for generic callers
- support provider-specific signature verification (e.g., Telegram `X-Telegram-Bot-Api-Secret-Token`) where applicable
- auth should be a FastAPI dependency so it is reusable across routers
- unauthenticated requests should be rejected with 401/403 before any task processing begins

Acceptance:
- unauthenticated requests to `/tasks` and `/webhook` are rejected
- authenticated requests proceed normally
- Telegram adapter uses provider-specific verification when available
- auth mechanism is configurable via environment variable, not hardcoded

---

## Milestone 12 - Observability + replay

### [x] T-090 Add task timeline
Track state transitions and important events.

Acceptance:
- one task's timeline visible in logs/API

### T-091 Add replay endpoint
Replay a prior task against same or new worker.

Acceptance:
- prior task can be replayed

### T-092 Add metrics
Track duration, success rate, retries, worker usage.

Acceptance:
- metrics exposed

---

## Milestone 13 - Hardening

### T-100 Secret scoping
Inject only minimum required secrets per run.

Acceptance:
- no global secret leakage into sandbox

### T-101 Add command safety policy
Require approval for dangerous/destructive commands.

Acceptance:
- dangerous commands, networked writes, and push/deploy actions map onto explicit permission classes

### T-113 Add paused-task approval decision endpoint
Add an explicit API for approving or rejecting tasks paused by orchestrator approval interrupts.

Scope notes:
- add `POST /tasks/{task_id}/approval` with payload `{ "approved": true|false }`
- support idempotent decision writes so repeated webhook/button deliveries do not double-apply decisions
- when approved, resume the interrupted orchestration run from checkpoint and continue normal lifecycle
- when rejected, terminally fail with a clear rejection summary and persist the decision metadata
- keep `/tasks/{id}` response shape stable; expose state transitions through existing task/run fields
- ensure queue/restart safety for decisions applied while workers or API processes restart

Acceptance:
- tasks paused for approval can be explicitly approved and continue to completion
- tasks paused for approval can be explicitly rejected and transition to terminal failure
- duplicate approval decision submissions are handled safely (no duplicate resumes)
- `/tasks/{id}` reflects pause -> decision -> terminal lifecycle transitions
- integration tests cover pause -> approve -> completed and pause -> reject -> failed

### T-102 Add quotas and budgets
Add broader global quotas and unattended limits beyond the runtime-local budget enforcement added earlier.

Acceptance:
- over-budget tasks fail safely
- unattended/background execution has stricter runtime limits than interactive execution

### T-103 Retention policy
Add cleanup and retention for workspaces and artifacts.

Acceptance:
- stale resources cleaned up automatically

### T-105 Auto-run repo lint/format after worker completion
Automatically run the repo's linter and formatter after the worker finishes but before verification, so the verifier and any resulting PR don't fail on style issues the model introduced.

Scope notes:
- detect the repo's lint/format commands from config files (e.g., `ruff format .`, `npm run lint --fix`, `Makefile` targets) or fall back to a configurable default
- run inside the existing sandbox session after the worker declares completion
- capture lint/format output as an artifact
- if lint/format changes files, include those changes in the diff and artifact capture
- do not fail the task if lint/format itself errors — surface the failure in the verifier report
- this step runs unconditionally (not gated by permission); to prevent unintended changes, restrict the lint/format command to the set of files modified by the worker

Acceptance:
- lint/format runs automatically after worker completion on repos with detectable tooling
- lint-introduced file changes are captured in the diff and artifact set
- lint/format errors are surfaced in the verifier report, not swallowed
- repos with no detectable lint/format config skip this step cleanly
- unit tests verify detection, execution, and error handling

---

## Milestone 14 - Agent intelligence

### T-106 Add evaluation harness with frozen task suite
Build a repeatable benchmark harness so prompt, routing, and tool changes can be measured against a fixed set of tasks with known-good outcomes.

Scope notes:
- define a small frozen task suite (5-10 tasks across different repo types and difficulty levels)
- each task specifies: repo URL/fixture, task text, expected outcome criteria (files changed, tests pass, specific content present)
- harness runs all tasks through the real orchestrator path and scores pass/fail per task
- results are persisted as structured JSON for comparison across runs
- harness must be runnable locally and in CI
- do not require real API keys for the frozen suite — support a mock/replay adapter if needed
- this is an internal developer tool, not a user-facing feature

Acceptance:
- frozen task suite exists with at least 5 tasks
- harness runs all tasks and produces a structured pass/fail report
- two consecutive runs on the same code produce the same scores (deterministic evaluation)
- results are diffable across code changes

### T-108 Add planning/decomposition step for complex tasks
Add an optional planning phase where complex tasks are broken into ordered sub-steps before the worker begins execution.

Scope notes:
- add a `plan_task` step in the orchestrator between `classify_task` and `choose_worker`
- planning triggers only when the task classifier marks the task as complex (multi-file, ambiguous, or architectural)
- the plan is a structured list of sub-steps with expected outcomes, not free-form prose
- the worker receives the plan as part of its task context and can reference/update step status during execution
- plan is persisted as a task artifact for observability
- simple tasks skip planning entirely — no added latency for straightforward changes
- do not implement sub-task orchestration (parallel steps, sub-task spawning) yet — the plan is advisory context for a single worker run

Acceptance:
- complex tasks produce a structured plan before worker dispatch
- simple tasks bypass planning with no overhead
- the plan is visible in task artifacts and run logs
- worker system prompt includes the plan when present
- unit tests verify plan generation and bypass logic

### T-109 Add context condenser for long-running agent loops
Add an explicit strategy for managing the context window within a single worker run so that long-running tasks don't degrade as the observation history grows.

Scope notes:
- implement a condenser that summarizes older observations when the accumulated context approaches a configurable threshold
- condensed summaries preserve: key decisions made, files modified, errors encountered, and current working state
- the condenser runs between agent loop iterations, not during tool execution
- condensed context replaces raw observations in the prompt — the worker sees a summary of early work plus full detail of recent work
- keep the condenser deterministic and fast — it should not require an LLM call (use structured extraction from the observation history)
- if the context is within budget, no condensation happens (zero overhead for short tasks)

Acceptance:
- long-running worker loops (>10 iterations) maintain effective context without exceeding the model's context window
- condensed summaries preserve actionable state (files touched, errors, decisions)
- short tasks incur no condensation overhead
- unit tests verify condensation triggers, summary content, and budget enforcement

### T-110 Add structured failure taxonomy for routing and recovery
Classify worker and sandbox failures into typed categories so retry, reroute, and escalation decisions can branch on failure type instead of inspecting free-form error strings.

Scope notes:
- define a failure taxonomy enum covering at least: `compile`, `test`, `tool_runtime`, `sandbox_infra`, `timeout`, `budget_exceeded`, `permission_denied`, `context_window`, `provider_error`, `provider_auth`, `unknown`
- extend `WorkerResult` to carry a typed `failure_kind` field alongside the existing `status` and `stop_reason`
- the escalation policy in `worker_routing_policy.md` and the retry logic in the orchestrator should branch on `failure_kind` rather than string matching
- sandbox command failures should propagate a classified failure kind up through the worker result
- verifier failures should carry their own classification (e.g., `test_regression`, `scope_mismatch`, `risky_command`)
- keep the taxonomy flat and extensible — do not build a deep hierarchy

Acceptance:
- every `WorkerResult` with `status != success` carries a typed `failure_kind`
- orchestrator retry/reroute logic branches on `failure_kind`
- verifier failures are classified and surfaced with their own typed category
- unit tests verify classification for common failure scenarios (compile error, test failure, timeout, auth error)

### T-111 Add worker self-review step before declaring completion
Have the worker review its own `git diff` output against the task objective before declaring "done," catching logical errors that the deterministic verifier cannot detect.

Scope notes:
- after the worker's agent loop exits with a success indicator, generate the cumulative diff and feed it back to the model with a focused review prompt
- the review prompt should ask: does this diff satisfy the task? are there unintended changes? are there obvious logical errors?
- the model's self-review response is captured as a structured artifact (confidence score + issues found)
- if the self-review identifies issues, the worker can re-enter the agent loop for a bounded number of fix iterations (max 2)
- if the self-review passes, the worker returns its result normally
- self-review runs inside the existing worker budget — it is not a free extra step
- self-review is skippable via a worker constraint flag for tasks where speed matters more than correctness

Acceptance:
- worker generates and reviews its own diff before returning a success result
- self-review output is persisted as a structured artifact with the run
- identified issues trigger a bounded fix loop (max 2 iterations)
- self-review respects the existing budget ledger
- tasks can opt out of self-review via constraint
- unit tests verify the review-then-fix loop and the opt-out path

### T-112 Add context window preflight guard
Check estimated prompt size before sending requests to the provider API so oversized requests fail fast instead of wasting a round-trip and budget.

Scope notes:
- before each LLM call in the agent loop, estimate the total prompt token count (system prompt + conversation history + tool definitions)
- compare against the model's known context window limit (maintain a small model→context-window registry)
- if the estimate exceeds the limit, trigger the condenser (T-109) or fail with a typed `context_window` error rather than sending the request
- token estimation can be approximate (character-based heuristic) — it does not need to be exact
- log a warning when the prompt exceeds 80% of the context window as an early signal
- the guard must not add significant latency — it runs on local data, no API calls

Acceptance:
- oversized prompts are caught before the API call, not after a provider error
- the guard triggers condensation or returns a typed error
- a warning is logged when prompt size exceeds 80% of the context window
- the model→context-window registry covers at least the configured Codex and Gemini models
- unit tests verify the guard for under-limit, warning-threshold, and over-limit cases

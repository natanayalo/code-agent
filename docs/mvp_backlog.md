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

### T-041 Implement CodexWorker or ClaudeWorker
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

Acceptance:
- `build_system_prompt()` produces a well-structured prompt given a `WorkerRequest` and workspace path
- AGENTS.md injection and repo structure listing both work
- unit tests verify prompt assembly for various input combinations

### T-047 Implement multi-turn agent loop in worker
Replace the one-shot toy-script pattern with an iterative agent loop: prompt → LLM →
tool call → execute via persistent shell → feed observation back → loop.

Scope notes:
- add `workers/claude_worker.py` implementing the agent loop via Anthropic API
- build system prompt via T-046's `build_system_prompt()`
- start persistent container via T-045's container/session layer
- start with a single `execute_bash` tool (per mini-SWE-agent's proven bash-only approach)
- exit on: LLM final answer, max iterations, or budget/timeout exceeded
- the loop is a simple while-loop, not a nested LangGraph graph
- the worker must include its own inner-loop safety envelope (max iterations, worker-local timeout, and budget checks) so it never runs unbounded; T-042 adds the outer orchestrator-level timeout/cancel layer on top of this, but T-047 must be safe to run standalone

Acceptance:
- worker executes a real multi-step task (e.g., create a file, run a test, fix a failure)
- agent loop runs ≥2 iterations, demonstrating observe-then-act behavior
- timeout/budget limits terminate the loop safely
- worker returns a clean `WorkerResult` (not an exception) when the budget or timeout is exceeded
- `WorkerResult` accurately reflects commands run, files changed, and final status

### T-042 Add baseline worker timeout/cancel handling
Add the outer orchestrator-level timeout and cancellation behavior required so the first real worker execution path cannot hang indefinitely even if the worker's own inner-loop guards are exhausted or fail to stop progress.

Acceptance:
- hung worker or sandbox execution fails safely within a configured timeout enforced by the orchestrator path
- timeout/cancel failure is surfaced back to the orchestrator without blocking the run forever
- workspace/logs are preserved for debugging after timeout

### T-044 Run one real orchestrator-to-worker vertical slice
Execute one real task submitted via curl, routed through orchestrator, run by the multi-turn agent worker in a real workspace, with results persisted and returned.

Scope notes:
- builds on persistent sandbox (T-045), system prompt (T-046), and agent loop (T-047)
- uses the multi-turn agent worker, not the toy CodexWorker
- includes the minimal HTTP task submission endpoint needed for curl-based validation
- includes a basic task status/result retrieval endpoint (GET by task_id) so callers can poll for completion
- includes execution-path persistence wiring for task/status, final result fields, worker run metadata, and captured artifacts needed for submission + GET-by-task_id polling
- builds on the baseline timeout/cancel handling from T-042
- worker should complete at least one multi-step coding task end-to-end
- Telegram is explicitly out of scope for this milestone
- hardcoded repo URL and task text are acceptable
- no mocks or fake worker results
- structured memory repository wiring remains out of scope here; `load_memory` / `persist_memory` stay stubbed until T-060..T-064
- result *delivery* (push-based, e.g. Telegram reply or webhook callback) is out of scope here and covered by the Telegram ingress milestone (T-050..T-053)

Acceptance:
- `curl` submission reaches the minimal HTTP task endpoint and starts a real run
- orchestrator routes to the implemented real worker
- worker executes a multi-step task in a real workspace via the agent loop and returns real output
- final result and run artifacts are persisted to DB
- API returns a task identifier and initial status for the asynchronous run
- completed result is retrievable via GET endpoint by task_id

---

## Milestone 6 - Telegram ingress (minimal real flow)

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

## Milestone 7 - Sandbox hardening

### T-054 Enforce sandbox execution boundary and destructive-action approval gate
Harden execution so one real command runs only inside the sandbox with complete artifact capture and approval interrupts for destructive actions.

Acceptance:
- one real command is executed in sandbox (not host process execution)
- stdout/stderr, changed files, and diff-summary artifacts are captured and persisted
- destructive command attempts hit the approval gate before execution
- approval pause/resume path is verified end-to-end
- milestone is only done when all checks pass without bypass flags

---

## Milestone 8 - Memory integration

### T-060 Add personal memory store
Store user preferences and approval/routing defaults.

Acceptance:
- personal memory can be created/read/updated

### T-061 Add project memory store
Store repo notes, successful commands, known pitfalls.

Acceptance:
- project memory can be created/read/updated

### T-062 Add memory retrieval policy
Load relevant memory before routing/dispatch.

Acceptance:
- second run on same repo sees prior memory context

### T-063 Add memory admin endpoints
Inspect and edit memory entries manually.

Acceptance:
- memory can be listed and modified

### T-064 Wire load_memory → execute → persist_learnings in orchestrator
Run the full memory loop on a real task execution path with structured records.

Acceptance:
- orchestrator loads structured memory before dispatch on a real task
- execution persists structured learnings back to memory stores
- stored memory is inspectable and retrievable via repositories/endpoints
- no opaque blob-only memory payloads are introduced

---

## Milestone 9 - Structured run observability + second worker routing

### T-043 Add structured run logs
Expand worker run metadata and output summaries beyond the baseline persistence required by T-044.

Acceptance:
- task run records include session_id, task_id, chosen worker, route reason, workspace id, start/end timestamps, final status, changed files count, and artifact list
- sandbox command records include command, exit code, duration, and stdout/stderr artifact locations
- structured run summaries are queryable in DB/logs without relying only on free-form text blobs

### T-070 Implement second worker adapter
Add remaining worker so both Claude and Codex are supported.

Acceptance:
- both workers runnable via same orchestrator path

### T-071 Add routing heuristics
Implement route policy and route-reason logging.

Acceptance:
- route decision stored with task

### T-072 Add manual worker override
Allow caller to pin a worker for a task.

Acceptance:
- override bypasses default routing

---

## Milestone 10 - Tools

### T-080 Add git utility wrapper
Expose git status, diff, branch, commit helpers.

Acceptance:
- worker/orchestrator can use git helper consistently

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
- at least one tool accessible through abstraction

---

## Milestone 11 - Observability + replay

### T-090 Add task timeline
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

## Milestone 12 - Hardening

### T-100 Secret scoping
Inject only minimum required secrets per run.

Acceptance:
- no global secret leakage into sandbox

### T-101 Add command safety policy
Require approval for dangerous/destructive commands.

Acceptance:
- destructive commands pause for approval

### T-102 Add quotas and budgets
Limit runtime, commands, file changes, artifacts.

Acceptance:
- over-budget tasks fail safely

### T-103 Retention policy
Add cleanup and retention for workspaces and artifacts.

Acceptance:
- stale resources cleaned up automatically

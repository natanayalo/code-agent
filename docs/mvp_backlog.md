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
Pick one first and integrate with real task execution.

Acceptance:
- toy repo task succeeds end-to-end

### T-042 Add worker timeout/cancel handling
Support max runtime and cancellation.

Acceptance:
- timed-out task fails safely
- workspace/logs preserved

### T-043 Add structured run logs
Store worker run metadata and output summary.

Acceptance:
- run details visible in DB/logs

---

## Milestone 5 - Message ingress

### T-050 Add generic webhook endpoint
Accept JSON task payload and create task/session.

Acceptance:
- posting webhook creates a task

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

---

## Milestone 6 - Memory v1

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

---

## Milestone 7 - Second worker + routing

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

## Milestone 8 - Tools

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

## Milestone 9 - Observability + replay

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

## Milestone 10 - Hardening

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

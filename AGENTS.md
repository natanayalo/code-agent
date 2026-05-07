# Coding Agent Contributor Guide

This repository builds a personal coding-agent service with:
- message/webhook ingress
- durable orchestration
- pluggable coding workers
- sandboxed repo execution
- structured memory
- progress + final result replies

The system is coding-first, not a generic assistant platform.

## Product goals

Build a service that can:
1. Receive a task from Telegram or HTTP webhook
2. Restore or create a session
3. Load relevant personal + project memory
4. Route the task to a coding worker
5. Run the task in an isolated workspace
6. Return progress updates and a final result
7. Persist useful learnings for future tasks

## Non-goals for v1

Do not build these yet:
- multi-user SaaS
- autonomous self-modifying production code
- consumer-facing platform beyond Telegram/Dashboard
- auto-merge / auto-deploy
- general-purpose voice assistant
- complex multi-agent swarms
- broad memory graph platform

## Architecture principles

1. Separate orchestration from execution
   - orchestrator owns workflow/state/routing/memory policy
   - worker owns repo task execution

2. Keep provider-specific logic isolated
   - all Gemini-specific logic stays in workers/gemini_cli_worker.py (and related adapters)
   - all Codex-specific logic stays in workers/codex_cli_worker.py (and related adapters)

3. Default to safe execution
   - use isolated workspaces
   - do not run worker tasks directly on host
   - require approval for destructive actions

4. Prefer structured state over prompt-only state
   - keep sessions/tasks/runs/artifacts in DB
   - keep memory structured and inspectable

5. Build thin interfaces
   - one Worker interface
   - one Sandbox interface
   - one Memory repository layer
   - one Tool boundary

## Golden rules

- Never change auth, secrets, billing, sandbox policy, or deployment permissions without explicit approval.
- Never add destructive automation by default.
- Never hardcode secrets or tokens.
- Never mix orchestration logic with provider-specific worker code.
- Every behavior change must have tests.
- Every new component must have logs that help debug failures.
- Prefer small commits and small PRs.
- Keep file changes focused.
- Do not introduce a new infrastructure dependency unless clearly justified.

## Repo-specific agent rules

- Before editing, inspect the relevant files and existing patterns.
- Prefer surgical changes. Do not refactor adjacent code unless required.
- For bug fixes, reproduce with a failing test or clear diagnostic first when practical.
- For PR tasks, keep the diff small and explain why each touched file changed.
- Run the narrowest relevant verification first, then broader tests if needed.
- Do not add new dependencies, config systems, abstractions, or feature flags unless explicitly required.
- If unsure, state the assumption and choose the safest minimal implementation instead of blocking, unless the ambiguity can cause data loss, security risk, or public API breakage.
- Preserve existing style, naming, logging, metrics, and error-handling conventions.

## Required development workflow

For each task:
1. Inspect relevant code first
2. Write a short implementation plan
3. Implement the smallest working slice
4. Add or update tests
5. Run verification
6. Summarize what changed, what was verified, and any follow-ups

## Python tooling environment

- Use **Poetry** for dependency management (`poetry install`).
- Use **Pytest** for testing with a **90% coverage threshold** enforced in CI (`--cov-fail-under=90`).
- Use the repository virtualenv explicitly for Python tooling and checks.
- Prefer `.venv/bin/...` invocations (for example: `.venv/bin/poetry`, `.venv/bin/pytest`, `.venv/bin/pre-commit`, `.venv/bin/ruff`, `.venv/bin/mypy`).
- Do not rely on globally installed `python`, `pytest`, or `pre-commit` binaries.

## Dashboard tooling environment

- Use **npm** for dependency management.
- Prefer `npm ci` in CI environments for reproducibility.
- Use **Vitest** for testing and maintain a **90% coverage threshold**.
- Run `npm run test:coverage` to verify dashboard changes.
- Ensure all new components have corresponding `*.test.tsx` files.

## Definition of done

A task is done only if:
- code compiles/runs
- tests pass
- logs are useful
- docs are updated if behavior changed
- rollback path is obvious
- no unrelated files were modified

## Agent assets

The `.agents/` directory contains supporting development workflows, rules, and repo-specific skills.

Use those assets to execute common tasks consistently, but treat this `AGENTS.md` file as the canonical source of repo policy. If any `.agents/` file conflicts with this document, follow `AGENTS.md`.

Prefer triggerable skills over workflow docs when the user request clearly matches one.

Current routing hints:
- use the `start-task` skill when the user asks to start a new task, pick the next backlog item, or begin the next implementation slice
- use the `address-review` skill, together with the GitHub review-comment skill when needed, when the user asks to inspect or address PR review feedback
- use the `db-schema` skill for model or migration changes under `db/`

## Folder ownership

### apps/
Entry points and application-layer logic (auth, progress, protocol mapping).
No core business domain logic here.

### orchestrator/
Owns workflow graph, state transitions, routing, retry policy, approval checkpoints.

### workers/
Owns coding execution adapters.
No session ownership here.

### sandbox/
Owns workspace creation, repo lifecycle, container execution, artifact capture.

### memory/
Entry point for memory-specific schemas and logic. Note: Current persistence logic for skeptical memory resides in `repositories/sqlalchemy.py`.

### tools/
Owns integration wrappers and tool abstractions.

### db/
Owns schema/migrations only.

### repositories/
Owns persistence access patterns, CRUD boundaries, and memory persistence logic.
No business policy here.

## Worker contract

All coding workers must implement the same interface.

Expected input:
- session_id
- repo_url
- branch
- task_text
- memory_context
- task_plan
- secrets
- tools
- constraints
- budget

Expected output:
- status
- summary
- failure_kind
- requested_permission
- budget_usage
- commands_run
- files_changed
- test_results
- artifacts
- review_result
- diff_text
- next_action_hint

Workers must not:
- mutate global orchestrator state directly
- write to memory directly
- bypass sandbox rules
- decide message delivery behavior

## Safety policy

### Allowed without approval
- read repo files
- run non-destructive build/test/lint commands
- modify files inside task workspace
- generate summaries and artifacts

### Requires approval
- deleting large file sets
- changing infra/security config
- running deploy commands
- accessing new secret scopes
- changing auth/billing/sandbox code
- destructive git operations
- network actions beyond allowlisted tools

## Memory policy

Use 3 categories only in v1:
- personal memory
- project memory
- session/thread state

Personal memory:
- user preferences
- routing preferences
- approval defaults
- communication preferences

Project memory:
- repo conventions
- successful commands
- architecture notes
- known pitfalls

**Skeletal Skepticism (T-060)**:
Memory is treated as a high-confidence "hint" rather than ground truth. Each entry must store:
- `source`: where the memory came from
- `confidence`: 0.0 to 1.0 score
- `scope`: application range (global, repo, branch)
- `last_verified_at`: when the information was last proved correct in a sandbox
- `requires_verification`: flag to force a skeptical re-check

**Compact Session State (T-061)**:
Preserves critical context across turns to maintain consistency:
- `active_goal`: the specific outcome currently being pursued
- `decisions_made`: key architectural or implementation choices recorded during the session
- `identified_risks`: potential pitfalls or constraints surfaced by the worker
- `files_touched`: unique list of files modified during the current session

Memory must be:
- structured
- inspectable
- editable
- deletable

Do not create opaque giant blobs of memory.

## Logging policy

Every task run must emit:
- session_id
- task_id
- worker_type
- workspace_id
- start/end timestamps
- final status
- files_changed
- artifact list

Every command run in sandbox must emit:
- command
- exit code
- duration
- stdout/stderr artifact locations

## Testing policy

Must have:
- unit tests for pure logic
- integration tests for orchestrator flow
- integration tests for sandbox runner
- one e2e happy path
- regression coverage for every critical-path bug fix

Critical-path test gate:
- Changes under `workers/`, `orchestrator/`, `repositories/`, or task-control API routes (for example `apps/api/routes/tasks.py`) must include:
  - targeted unit coverage for changed logic
  - integration coverage for changed state/API behavior
  - a focused e2e smoke covering the affected operator flow
- Changes under `db/migrations/` or timeline/state constraints must include migration-path integration coverage (upgrade plus write assertions).
- Changes to dashboard operator controls must include component coverage, API/service contract coverage, and an interaction-state smoke test.

Coverage expectations:
- Do not optimize for blanket 100% global line coverage.
- For critical-path slices, changed-line coverage target is `>=95%`.
- New branches in changed critical functions must be exercised by tests.
- If a critical-path behavior remains untested, do not merge without explicit documented risk acceptance and follow-up ownership.

Before merging:
- run unit + integration tests
- run required critical-path e2e smoke checks for touched areas
- run formatter/linter
- verify at least one happy path manually if behavior changed
- include the exact verification commands and outcomes in the PR description

## Priorities

Priority order and active task tracking are maintained in [docs/status.md](docs/status.md). For long-term vision and phase sequencing, see [docs/roadmap.md](docs/roadmap.md).

## Code style

- Python 3.12+
- typed code
- Pydantic for boundaries
- small functions
- explicit names
- avoid deep inheritance
- prefer composition

## Commit guidance

Commit messages should be specific and follow the commitizen format:
- `feat: add langgraph task state and checkpoint store`
- `feat: implement docker sandbox workspace runner`
- `feat: add codex worker adapter for repo task execution`

Avoid vague messages:
- `fix stuff`
- `update app`

## What to optimize for

Optimize for:
- reliability
- clarity
- inspectability
- safe iteration

Do not optimize for:
- premature abstraction
- speculative multi-agent complexity
- feature breadth over shipping

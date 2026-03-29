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
- broad multi-channel platform beyond Telegram + webhook
- auto-merge / auto-deploy
- general-purpose voice/consumer assistant
- complex multi-agent swarms
- broad memory graph platform

## Architecture principles

1. Separate orchestration from execution
   - orchestrator owns workflow/state/routing/memory policy
   - worker owns repo task execution

2. Keep provider-specific logic isolated
   - all Claude-specific logic stays in workers/claude_worker.py
   - all Codex-specific logic stays in workers/codex_worker.py

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

## Required development workflow

For each task:
1. Inspect relevant code first
2. Write a short implementation plan
3. Implement the smallest working slice
4. Add or update tests
5. Run verification
6. Summarize what changed, what was verified, and any follow-ups

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

## Folder ownership

### apps/
Entry points only.
No business logic here.

### orchestrator/
Owns workflow graph, state transitions, routing, retry policy, approval checkpoints.

### workers/
Owns coding execution adapters.
No session ownership here.

### sandbox/
Owns workspace creation, repo lifecycle, container execution, artifact capture.

### memory/
Owns structured memory persistence and retrieval.

### tools/
Owns integration wrappers and tool abstractions.

### db/
Owns schema/migrations only.

### repositories/
Owns persistence access patterns and CRUD boundaries.
No business policy here.

## Worker contract

All coding workers must implement the same interface.

Expected input:
- session_id
- repo_url
- branch
- task_text
- memory_context
- constraints
- budget

Expected output:
- status
- summary
- commands_run
- files_changed
- test_results
- artifacts
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

Session state:
- task progress
- worker choice
- run history for current thread

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
- chosen worker
- route reason
- workspace id
- start/end timestamps
- final status
- changed files count
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

Before merging:
- run unit + integration tests
- run formatter/linter
- verify at least one happy path manually if behavior changed

## Priorities

Priority order:
1. single end-to-end happy path
2. safe isolation
3. durable state
4. useful logs
5. second worker
6. memory refinement
7. more tools/channels

## Code style

- Python 3.12+
- typed code
- Pydantic for boundaries
- small functions
- explicit names
- avoid deep inheritance
- prefer composition

## Commit guidance

Commit messages should be specific:
- `add langgraph task state and checkpoint store`
- `implement docker sandbox workspace runner`
- `add codex worker adapter for repo task execution`

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

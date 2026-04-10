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

Prefer triggerable skills over workflow docs when the user request clearly matches one.

Current routing hints:
- use the `start-task` skill when the user asks to start a new task, pick the next backlog item, or begin the next implementation slice
- use the `address-review` skill, together with the GitHub review-comment skill when needed, when the user asks to inspect or address PR review feedback
- use the `db-schema` skill for model or migration changes under `db/`

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
1. [DONE] Milestone 3: Artifact capture baseline (T-032)
2. [DONE] Milestone 4: First worker interface + implementation (T-040/T-041)
3. [DONE] Milestone 5: Vertical Slice E2E milestone (T-042 + T-044)
4. [DONE] Milestone 6: Sandbox hardening milestone (T-054/T-055)
5. [DONE] Milestone 7: Memory integration milestone (T-060 to T-065)
6. [DONE] Milestone 8: Structured run observability (T-043) + Milestone 9: T-070 second worker (GeminiCliWorker)
7. [DONE] Milestone 9 (remaining): T-071 routing heuristics + T-072 manual override
8. [DONE] Milestone 10: Telegram ingress milestone (T-050 to T-053)
9. [DONE] Review follow-ups: T-084, T-085, T-086
10. T-104: API authentication (pulled from Milestone 13 — close auth gap before adding capabilities)
11. Milestone 11: External tool wrappers and MCP compatibility (T-083, T-080, T-087, T-081, T-082, T-088, T-089, T-107)
12. Milestone 12: Observability + replay (T-090 to T-092)
13. Milestone 13 (remainder): Hardening (T-100 to T-103, T-105)
14. Milestone 14: Agent intelligence (T-106, T-108, T-109, T-110, T-111, T-112)

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

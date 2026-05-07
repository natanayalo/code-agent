---
name: start-task
description: Use when the user asks to start a new task, begin the next implementation slice or PR, or pick the next roadmap item from docs/roadmap.md and docs/status.md.
---

# Start Task Skill

Use this skill to turn a fresh task request into the smallest safe implementation slice.

## Read first

- `AGENTS.md`
- `README.md`
- `docs/roadmap.md`
- `docs/status.md`
- nearby code, tests, and config for the task area

## Trigger rules

- If the user names a specific task, scope the work to that task.
- If the user says "start a new task" or equivalent without naming one, choose the first unfinished item from the `Current Backlog` in `docs/status.md`.
- If `docs/status.md` is empty or ambiguous, fall back to the first unfinished item in the current phase from `docs/roadmap.md`.

## Required workflow

1. Inspect the canonical docs and relevant code first.
2. State a short plan and the likely files to change.
3. Call out the main risk and the smallest safe scope.
4. Implement the narrowest working slice.
5. Add or update tests in the same slice.
6. Run layered verification for the changed behavior: changed-file lint/type checks, targeted unit tests, required integration tests, and required e2e smoke checks for touched critical paths. After targeted checks pass, run the broader suite (`pytest` or `npm run test:run`) before publishing. Use repo-standard entrypoints (for Python prefer `.venv/bin/...`). If a command needs approval or is blocked, request approval or report the blocker clearly.
7. Create or switch to a focused task branch before publishing work. Prefer `task/<task-id>-<short-slug>` when a task ID exists; otherwise use `task/<short-slug>`.
8. Update `docs/status.md` before wrapping up:
   - move a finished task slice out of `Next` or `In Progress`
   - add it to `Done` with the PR number once the work is published
   - leave the task in `In Progress` only when the slice is still actively unfinished or unpublished
9. Stage only the task files, create a specific commit, push the branch, and create a Pull Request (PR) by default once checks pass; skip publish steps only if the user explicitly asks not to publish. Use `gh pr create` with a descriptive title and body.
10. Update `README.md` or other nearby instructions when local workflow, verification, or CI/CD behavior changes.
11. Call out any manual external follow-up that cannot be enforced from repo code, then summarize what changed, what was verified, what was deferred, and provide the PR link.

## Critical-Path Test Matrix (Required)

- If you touch `workers/`, `orchestrator/`, `repositories/`, or task-control API routes such as `apps/api/routes/tasks.py`:
  - add or update unit tests for changed logic
  - add or update integration tests for the changed behavior and state transitions
  - run a focused e2e smoke (`tests/integration/test_vertical_slice_e2e.py` or equivalent) that covers the affected operator flow
- If you touch `db/migrations/` or timeline/state constraints:
  - add or update migration integration tests (upgrade path plus write-path assertions)
  - add or update integration coverage for the affected orchestrator/repository behavior
- If you touch dashboard operator controls under `dashboard/src/`:
  - add or update component tests
  - add or update service/API contract coverage
  - add one interaction-state smoke test for the changed control path

## Regression-First Rule

- For bug fixes and regressions, add or update a failing test (or capture a clear failing diagnostic) before implementing the fix when practical.
- If you cannot write a failing test first, explain why and still add the strongest regression test immediately after the fix.

## Coverage Guardrails

- Do not treat global 100% line coverage as the main quality gate.
- For critical-path slices, require high changed-line coverage (target `>=95%`) plus tests for all newly introduced branches in changed critical functions.
- Do not publish if a known critical-path behavior is untested unless the risk is explicitly documented in the PR summary with follow-up ownership.

## Basic Use-Case Checklist (Critical-Path PRs)

- Task creation succeeds and enters the expected state.
- Human interaction request/response flow works and resumes correctly.
- Cancellation works for queued and running tasks with terminal semantics.
- Final status and timeline events remain consistent with API responses.
- Dashboard state and tracing data reflect the run outcome without contradictions.

## Scope guardrails

- Prefer small, reviewable diffs.
- Do not absorb adjacent cleanup unless it is required for correctness.
- If review or design feedback expands scope, track a follow-up instead of silently widening the task.
- Update `docs/status.md` when task progress materially changes, and explicitly move finished published tasks into `Done`.
- Do not describe branch protection or similar external controls as enforced unless the required
  GitHub or platform settings are also called out.
- Keep commits focused and do not stage unrelated changes.
- Do not push until the requested checks pass or the blocker is explicitly called out.

## Response shape

For non-trivial work, use:

1. Plan
2. Risk check
3. Edits
4. Verification
5. Summary

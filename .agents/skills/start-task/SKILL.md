---
name: start-task
description: Use when the user asks to start a new task, begin the next implementation slice or PR, or pick the next backlog item from docs/status.md and docs/mvp_backlog.md.
---

# Start Task Skill

Use this skill to turn a fresh task request into the smallest safe implementation slice.

## Read first

- `AGENTS.md`
- `README.md`
- `docs/status.md`
- `docs/implementation_order.md`
- `docs/mvp_backlog.md`
- nearby code, tests, and config for the task area

## Trigger rules

- If the user names a specific task, scope the work to that task.
- If the user says "start a new task" or equivalent without naming one, prefer the first item under `Next` in `docs/status.md`.
- Do not jump ahead of `docs/implementation_order.md`.

## Required workflow

1. Inspect the canonical docs and relevant code first.
2. State a short plan and the likely files to change.
3. Call out the main risk and the smallest safe scope.
4. Implement the narrowest working slice.
5. Add or update tests in the same slice.
6. Run the minimal checks and tests that cover the changed behavior, including `pre-commit` and the relevant `pytest` checks. If a command needs approval or is blocked, request approval or report the blocker clearly.
7. Create or switch to a focused task branch before publishing work. Prefer `task/<task-id>-<short-slug>` when a task ID exists; otherwise use `task/<short-slug>`.
8. Stage only the task files, create a specific commit, and push the branch when the user wants the slice published.
9. Update `README.md` or other nearby instructions when local workflow, verification, or CI/CD behavior changes.
10. Call out any manual external follow-up that cannot be enforced from repo code, then summarize what changed, what was verified, and what was deferred.

## Scope guardrails

- Prefer small, reviewable diffs.
- Do not absorb adjacent cleanup unless it is required for correctness.
- If review or design feedback expands scope, track a follow-up instead of silently widening the task.
- Update `docs/status.md` when task progress materially changes.
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

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
6. Propose the minimal verification commands, but do not run them without approval.
7. Summarize what changed, what was verified, and what was deferred.

## Scope guardrails

- Prefer small, reviewable diffs.
- Do not absorb adjacent cleanup unless it is required for correctness.
- If review or design feedback expands scope, track a follow-up instead of silently widening the task.
- Update `docs/status.md` when task progress materially changes.

## Response shape

For non-trivial work, use:

1. Plan
2. Risk check
3. Edits
4. Verification
5. Summary

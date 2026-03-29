# Workflow: Start Task

Use this workflow for new implementation tasks.

## Goal

Translate a request or backlog item into the smallest safe implementation slice.

## Steps

1. Inspect the canonical docs.
   - Read `AGENTS.md`.
   - Read `README.md` if local setup or workflow may matter.
   - Check `docs/implementation_order.md` and `docs/status.md` so you do not jump ahead.
   - Read the specific backlog item in `docs/mvp_backlog.md` if the task is milestone-driven.

2. Inspect the relevant code.
   - Read nearby entrypoints, models, tests, and config first.
   - Prefer existing patterns over new abstractions.

3. Write a short plan.
   - State the narrow scope.
   - Name the files likely to change.
   - Call out the intended verification commands before running them.

4. Define the smallest slice.
   - Keep behavior changes tightly scoped.
   - Defer follow-up work explicitly instead of silently absorbing it.

5. Implement.
   - Keep diffs focused.
   - Add or update tests in the same slice.
   - Avoid unrelated cleanup.

6. Verify.
   - Prefer targeted `pre-commit` and targeted tests first.
   - Escalate to broader verification only when the change scope justifies it.

7. Summarize.
   - What changed
   - What was verified
   - What was deferred or needs follow-up

## Exit checklist

- relevant code was inspected first
- plan was stated
- smallest slice was implemented
- tests were added or updated
- verification was run or explicitly deferred
- follow-ups are tracked if needed

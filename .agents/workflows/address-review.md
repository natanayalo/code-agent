# Workflow: Address Review

Use this workflow when a PR has review feedback.

## Goal

Handle actionable review comments without widening the PR unnecessarily.

## Steps

1. Collect the latest review feedback.
   - Separate new comments from older thread history.
   - Distinguish blocking issues from optional cleanup.

2. Cluster comments by outcome.
   - correctness or safety bug
   - test or verification gap
   - maintainability cleanup
   - scope-expanding suggestion

3. Keep the PR focused.
   - Fix blocking correctness or safety issues in the current PR.
   - Take small maintainability fixes if they are clearly local.
   - Defer scope-expanding design changes into an explicit follow-up task when appropriate.

4. Inspect the affected code before editing.
   - Read the current file state, not just the review snippet.
   - Check whether prior local fixes already address the comment.

5. Patch the smallest safe set of files.
   - Keep each change traceable back to a review point.
   - Do not smuggle in unrelated improvements.

6. Re-run targeted verification.
   - `pre-commit run --files <changed files>`
   - the narrowest relevant tests

7. Summarize.
   - addressed comments
   - intentionally deferred comments
   - evidence from verification

## Review triage rule

Do not automatically accept every suggestion.
If a comment conflicts with repo tradeoffs or expands scope, explain why and track a follow-up task instead.

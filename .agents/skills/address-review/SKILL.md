---
name: address-review
description: Use when the user asks to inspect or address pull request review comments, requested changes, or new GitHub PR feedback. Pair with the GitHub review skill for comment collection, then apply repo-specific triage and smallest-safe fixes.
---

# Address Review Skill

Use this skill for repo-specific triage after pull request review feedback is collected.

## Read first

- `AGENTS.md`
- the current file state for each commented file
- `docs/status.md` and `docs/mvp_backlog.md` if a comment suggests a follow-up task

## Collection rule

- For GitHub PR review comments, use the GitHub review-comment skill or GitHub app tooling to collect the latest feedback first.
- Read the current local file, not just the review snippet.

## Triage workflow

1. Separate new comments from old thread history.
2. Classify each comment:
   - correctness or safety bug
   - test or verification gap
   - maintainability cleanup
   - scope-expanding design suggestion
3. Fix blocking correctness or safety issues in the current PR.
4. Take small local maintainability fixes only when they stay tightly scoped.
5. Defer broader design changes into an explicit follow-up task when needed.

## Patch guardrails

- Keep each edit traceable to a review point.
- Do not smuggle unrelated improvements into a review-fix commit.
- Check whether the current branch already satisfies a comment before patching.

## Verification pattern

- Propose targeted `pre-commit run --files <changed files>`
- Propose the narrowest relevant tests
- Do not run commands without approval

## Summary expectations

Report:
- which comments were addressed
- which comments were intentionally deferred
- what verification was run or proposed

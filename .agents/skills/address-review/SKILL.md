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

## Replying and Resolving Threads

- To reply to and resolve individual inline GitHub review comments natively, use the `gh api graphql` command.
- Always prefix `gh api graphql` with `GH_PAGER=cat` to suppress the interactive pager in automation.
- Note: On macOS, the `gh` binary may be located at `/opt/homebrew/bin/gh`. If `gh` is not in your PATH, use the full path.
- Find thread IDs using: `GH_PAGER=cat gh api graphql -F owner="{owner}" -F name="{repo}" -F pr={PR_NUMBER} -f query='query($owner: String!, $name: String!, $pr: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $pr) { reviewThreads(first: 20) { nodes { id isResolved comments(first: 5) { nodes { body author { login } } } } } } } }'`
  - Use `first: 20` for threads (not 10) and `first: 5` for comments so full thread history (including prior replies) is visible.
  - Check `isResolved` and read the full comment thread before patching — a previous session may have already fixed it.
- Reply and resolve a specific thread: `GH_PAGER=cat gh api graphql -F id="{THREAD_ID}" -F body="{REPLY_TEXT}" -f query='mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { id } } resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'`

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
- Check whether the current branch already satisfies a comment before patching. If `git status` shows a clean tree, check `git log --oneline -5` to confirm whether the fix was committed in an earlier step.

## Verification pattern

- Run `pytest` on the affected unit/integration tests after each fix — this is always safe in this repo per AGENTS.md.
- Run `pre-commit run --files <changed files>` when the change touches linted code.
- Do not run deploy commands or destructive operations without approval.

## Finalizing the Session

After all review threads have been addressed, committed, pushed, and resolved:
- Add a new top-level comment to the PR to trigger a fresh review:
  `GH_PAGER=cat gh pr comment {PR_NUMBER} --body "@gemini-code-assist review"`

## Summary expectations

Report:
- which comments were addressed
- which comments were intentionally deferred
- what verification was run or proposed

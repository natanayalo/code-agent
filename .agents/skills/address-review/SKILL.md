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
- Find thread IDs using: `gh api graphql -F owner="{owner}" -F name="{repo}" -F pr={PR_NUMBER} -f query='query($owner: String!, $name: String!, $pr: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $pr) { reviewThreads(first: 10) { nodes { id isResolved comments(first: 1) { nodes { body } } } } } } }'`
- Reply and resolve a specific thread: `gh api graphql -F id="{THREAD_ID}" -F body="{REPLY_TEXT}" -f query='mutation($id: ID!, $body: String!) { addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body}) { comment { id } } resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'`

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

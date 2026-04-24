---
name: address-review
description: Use when the user asks to address GitHub PR review comments, requested changes, or reviewer feedback by triaging comments, applying smallest-safe fixes, verifying, and replying/resolving threads.
---

# PR Review Addressor Skill

Use this skill to address pull request review feedback after comments are collected from GitHub.

## Goal

Resolve reviewer feedback with the smallest safe changes.

For each review comment:
1. Understand the actual concern.
2. Verify whether it still applies to the latest PR head.
3. Fix only what is necessary.
4. Run targeted verification.
5. Reply with what changed.
6. Resolve the thread only when appropriate.

## Modes

Default mode is local-fix mode:

- Triage comments.
- Apply local changes.
- Run verification.
- Report suggested replies.
- Do not commit, push, reply, resolve, or trigger re-review.
- Never call GitHub mutation APIs in this mode. Produce proposed reply text in the final summary instead.

End-to-end mode is active only when the user explicitly asks to address comments fully, push fixes, reply/resolve threads, or otherwise complete the PR review cycle.

## Read First

- Repository instructions such as `AGENTS.md`, `CLAUDE.md`, `.cursor/rules`, or equivalent if present.
- Current file state for each commented file.
- Latest PR diff and current PR head.
- Existing review thread history, including author replies and resolved state.
- Directly related tests, fixtures, callers, imports, and symbols.
- Project planning docs only when comments are explicitly about scope, backlog, or follow-up work.

## Repository Safety Checks

Before editing:

- Discover repository owner/name:
  - `OWNER_REPO="$(GH_PAGER=cat gh repo view --json nameWithOwner --jq .nameWithOwner)"`
  - `OWNER="${OWNER_REPO%/*}"`
  - `REPO="${OWNER_REPO#*/}"`
- Confirm branch: `git branch --show-current`.
- Confirm PR association using provided PR number/URL or branch-based discovery:
  - `BRANCH="$(git branch --show-current)"`
  - `GH_PAGER=cat gh pr list --head "$BRANCH" --state open --limit 20 --json number,url,title,headRefName,baseRefName,state,updatedAt`
  - If exactly one PR matches, use it.
  - If multiple PRs match, select the most recently updated.
  - If no PR matches, fail with a clear actionable message.
- Check local state: `git status --short`.
- Do not overwrite unrelated user changes.
- If the working tree is dirty, determine whether changes are from this session before editing.
- Do not run destructive commands, deploy commands, or broad cleanup commands without explicit approval.

## Collection Rule

- For GitHub PR review comments, use GitHub tooling to collect latest feedback first.
- Always read current local file state or latest PR file content, not only the review snippet.

Collect:

- Inline review comments.
- Top-level PR comments when relevant.
- Submitted reviews.
- Review threads and resolved state.
- Latest commits since comments were created.

## Stale Comment Guard

Before acting on each comment:

- Check whether thread is already resolved.
- Check whether commented code still exists at latest PR head.
- Check whether later commit already fixed it.
- Check whether concern still applies to current behavior.

If already fixed, reply with concise evidence and resolve only when appropriate.

## Triage Workflow

For each comment, classify as:

- Correctness or safety bug.
- Test or verification gap.
- Maintainability cleanup.
- Scope-expanding design suggestion.
- Stale or already fixed.
- Not applicable.

For fixes in scope:

1. Reproduce or reason through the issue.
2. Add/update the smallest useful regression test when practical.
3. Apply the smallest safe code change.
4. Run targeted verification.
5. Commit the fix only when end-to-end mode is requested.
6. Push only after verification passes and only in end-to-end mode.
7. Reply to thread with what changed only in end-to-end mode.
8. Resolve thread only after fix/reply addresses concern and only in end-to-end mode unless explicitly requested otherwise.

For out-of-scope or design suggestions:

1. Reply with reasoning, tradeoff, or deferral plan.
2. Resolve only if reply fully addresses concern and team norms allow author resolution.
3. Otherwise leave unresolved and report as awaiting reviewer confirmation.
4. Do not resolve threads where the latest reviewer message asks a question, expresses disagreement, or requests confirmation unless explicitly asked to resolve.

## Patch Guardrails

- Keep edits traceable to specific review points.
- Do not smuggle unrelated improvements into review-fix commits.
- Prefer one focused commit per review theme unless repo style prefers squashed commits.
- Do not broaden scope unless the comment requires it.
- If a comment asks for redesign, propose the smallest safe alternative first.

## Verification Pattern

Run the narrowest meaningful verification:

- Affected unit tests.
- Affected integration tests when needed.
- Lint/pre-commit for changed files.
- Type checks when touched code is typed and repo uses them.

Choose exact commands from repository instructions/tooling, not assumptions.

Do not assume `pytest` or `pre-commit` exist unless project files/instructions indicate they do.
Never run deploy commands or destructive operations without approval.

## Replying and Resolving Threads

Use GitHub GraphQL for native replies and thread resolution.

- Always prefix GitHub CLI calls with `GH_PAGER=cat`.
- On macOS, if `gh` is not on PATH, use `/opt/homebrew/bin/gh`.

Find thread IDs:

```bash
GH_PAGER=cat gh api graphql \
  -F owner="{owner}" \
  -F name="{repo}" \
  -F pr={PR_NUMBER} \
  -f query='
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 50) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          path
          line
          comments(first: 20) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              body
              author { login }
              createdAt
              url
            }
          }
        }
      }
    }
  }
}'
```

If `pageInfo.hasNextPage` is true for threads or comments, paginate until all relevant unresolved threads are collected.

Reply to a thread:

```bash
GH_PAGER=cat gh api graphql \
  -F id="{THREAD_ID}" \
  -F body="{REPLY_TEXT}" \
  -f query='
mutation($id: ID!, $body: String!) {
  reply: addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $id,
    body: $body
  }) {
    comment { id url }
  }
}'
```

Reply format:

- Fixed in `<commit>` by `<brief change>`.
- Verified with `<command/result>`.
- If not fixed: explain why deferred/not applicable and what follow-up exists.

Resolve a thread after reply/fix is complete:

```bash
GH_PAGER=cat gh api graphql \
  -F id="{THREAD_ID}" \
  -f query='
mutation($id: ID!) {
  resolve: resolveReviewThread(input: {
    threadId: $id
  }) {
    thread { id isResolved }
  }
}'
```

## Final Review Trigger

Post automated re-review trigger only when requested by user or documented by repo instructions.

Example:

```bash
GH_PAGER=cat gh pr comment {PR_NUMBER} --body "@gemini-code-assist review"
```

Do not post bot triggers by default in unknown repositories.

## Final Summary

Report:

- PR URL/number.
- Branch.
- Comments addressed.
- Comments deferred.
- Comments already fixed or stale.
- Commits created.
- Push status.
- Threads replied to.
- Threads resolved.
- Verification commands run and results.
- Final repository state from `git status --short` (clean or remaining changes).
- Remaining reviewer action needed.

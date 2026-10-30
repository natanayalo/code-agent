---
name: start-task
description: Start a named repository task or select the next ready implementation slice from docs/status.md, then verify and publish a focused PR.
---

# Start task

Follow [AGENTS.md](../../../AGENTS.md) for permissions, ownership, test gates,
commit rules, and completion requirements.

## Select scope

- Honor the user's named task and existing constraints.
- Otherwise choose the first ready slice in the active implementation queue in
  [current status](../../../docs/status.md#current-backlog). Maintenance and
  evidence/approval-gated lanes are separate; do not promote them automatically.
- Use the roadmap for milestone boundaries, not to rerun completed milestone
  specifications. Read only the relevant issue, docs, code, and tests.

## Execute and deliver

1. Inspect local changes and existing patterns, then state the smallest plan
   and material risk. Reuse a suitable task branch/PR for continued work;
   otherwise create a focused branch using the configured branch prefix.
2. Implement the bounded slice. For a bug, capture a failing test or clear
   diagnostic first when practical. Preserve unrelated user changes.
3. Add the behavior and critical-path coverage required by AGENTS.md. Run the
   narrowest checks first, followed by the required broader suite. Use
   `.venv/bin/...` for Python tooling and npm in dashboard/. For documentation
   or instruction-only edits, validate affected links, commands, and skill
   metadata and report that runtime suites were not rerun.
4. Update status when priorities or task state change; keep merged history in
   CHANGELOG.md. Update operator/setup docs when their behavior changes.
5. Stage only task files, commit with normal hooks, and push after verification.
   Open or update a PR by default unless the user requested local-only work.
   Prefer available GitHub integration tools; use the CLI when needed. Include
   exact verification commands/outcomes and any external follow-up or limitation.
6. Report the PR, changes, checks, and remaining work. Do not mark deferred or
   approval-gated behavior complete.

Avoid adjacent refactors or policy changes. If a blocker prevents required
verification, explain it and the remaining scope rather than implying success.

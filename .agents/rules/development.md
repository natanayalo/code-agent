# Development Rule

Use this rule for normal implementation work in this repository.

## Source of truth

Follow these in order:
1. `AGENTS.md`
2. `docs/roadmap.md`
3. `docs/status.md`
4. nearby code and tests

If this rule conflicts with `AGENTS.md`, follow `AGENTS.md`.

## Required task flow

Every non-trivial task must follow this sequence:
1. Inspect relevant code, docs, and nearby tests first.
2. Write a short implementation plan.
3. Implement the smallest working slice.
4. Add or update tests at the right level.
5. Run targeted verification.
6. Summarize what changed, what was verified, and any follow-ups.

## Scope control

- Keep changes small and reviewable.
- Do not pull work from a later roadmap phase unless explicitly asked.
- Do not mix unrelated repo-ops, docs, and feature work in one PR unless the task explicitly requires it.
- Keep file ownership boundaries intact:
  - `apps/` entrypoints only
  - `db/` schema and migrations only
  - `repositories/` persistence access patterns only
  - `workers/` provider-specific execution only
  - `orchestrator/` workflow and state only

## Safety checks

- Do not change auth, billing, secrets, sandbox policy, or deployment permissions without explicit approval.
- Do not add destructive automation by default.
- Do not hardcode credentials or tokens.
- Prefer targeted verification over broad expensive checks.
- Do not mark a task done if tests or verification were skipped; call that out explicitly.

## Documentation checks

- Update docs if behavior or repo workflow changes.
- Update `README.md` when local setup, verification, CI/CD, or developer workflow changes.
- Update `docs/status.md` when task state or sequencing changes.
- If a change depends on external settings that are not stored in the repo, document the manual
  follow-up clearly and do not describe it as repo-enforced.
- Add follow-up roadmap/status notes when you intentionally defer a real design decision.

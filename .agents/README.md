# .agents

This directory contains development-oriented agent assets for this repository.

These files do not replace the canonical repo guidance. They operationalize it.

## Canonical sources

Always defer to these files if guidance overlaps:
- `AGENTS.md`
- `README.md`
- `docs/implementation_order.md`
- `docs/mvp_backlog.md`
- `docs/status.md`

## Purpose

Use `.agents/` to make repeated development work more consistent:
- start a scoped implementation task
- address PR review feedback
- make schema-only DB changes safely

## Layout

- `rules/`: thin operating rules that point back to the canonical docs
- `workflows/`: step-by-step execution checklists for common development loops
- `skills/`: focused repo-specific guidance for specialized areas

## Current assets

- `rules/development.md`
- `workflows/start-task.md`
- `workflows/address-review.md`
- `workflows/schema-change.md`
- `skills/db-schema/SKILL.md`

## Scope

Keep these files thin.
Do not duplicate broad project policy that already exists in `AGENTS.md`.

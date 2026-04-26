# .agents

This directory contains development-oriented agent assets for this repository.

These files do not replace the canonical repo guidance. They operationalize it.

## Canonical sources

Always defer to these files if guidance overlaps:
- `AGENTS.md`
- `README.md`
- `docs/roadmap.md`
- `docs/status.md`

## Purpose

Use `.agents/` to make repeated development work more consistent:
- start a scoped implementation task
- address PR review feedback
- make schema-only DB changes safely

## Layout

- `rules/`: thin operating rules that point back to the canonical docs
- `workflows/`: thin reference checklists and pointers
- `skills/`: triggerable repo-specific guidance for repeated tasks and specialized areas

## Current assets

- `.agents/rules/development.md`
- `.agents/skills/start-task/SKILL.md`
- `.agents/skills/address-review/SKILL.md`
- `.agents/skills/db-schema/SKILL.md`
- `.agents/skills/dashboard/SKILL.md`
- `.agents/workflows/start-task.md`
- `.agents/workflows/address-review.md`
- `.agents/workflows/schema-change.md`

## Scope

Keep these files thin.
Do not duplicate broad project policy that already exists in `AGENTS.md`.

Prefer `skills/` for procedures that should trigger from matching user requests.
Keep `workflows/` as human-readable pointers or supplemental checklists only.

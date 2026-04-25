---
name: db-schema
description: Project-specific guidance for SQLAlchemy and Alembic schema work in this repository.
---

# DB Schema Skill

Use this skill when a task changes database models or migrations.

## Canonical references

Read these first:
- `AGENTS.md`
- `docs/roadmap.md`
- `docs/status.md`
- `db/base.py`
- `db/models.py`
- `db/migrations/`

## Current conventions

- Put schema and migration work under `db/` only.
- Use `Base` from `db/base.py`.
- Use `UUIDPrimaryKeyMixin` for string UUID primary keys.
- Use `TimestampMixin` for `created_at` and `updated_at`.
- Use explicit names and small model classes.
- Prefer JSON columns for structured payloads already described by the architecture docs.

## Migration conventions

- Create explicit Alembic migration files in `db/migrations/versions/`.
- Keep upgrade and downgrade paths readable.
- Avoid hidden autogeneration assumptions in reviewable PRs.
- Preserve compatibility unless the task explicitly owns a breaking migration.

## Test conventions

For schema tasks, aim for:
- one metadata-level unit test
- one Alembic migration application test

Only add runtime DB integration coverage when the task actually needs repository or app behavior.

## Scope guardrails

- Do not add repository layer code in schema-only tasks.
- Do not add app DB wiring in schema-only tasks.
- Do not decide new enum vocabularies casually.

If the task touches persisted statuses or constrained value fields, check whether it belongs in `T-013` instead of absorbing the decision into the current PR.

## Verification pattern

Start with:
- `pre-commit run --files <changed schema files>`
- `pytest tests/unit/test_db_models.py tests/integration/test_db_migrations.py`

If real Postgres compatibility matters, add:
- `DATABASE_URL=... alembic upgrade head`

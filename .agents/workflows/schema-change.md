# Workflow: Schema Change

Use this workflow for changes under `db/` or for persistence schema tasks.

## Goal

Make schema changes that are explicit, testable, and reversible.

## Steps

1. Inspect the current schema surface.
   - Read `db/base.py`, `db/models.py`, and the relevant migration files.
   - Check `docs/mvp_backlog.md` and `docs/status.md` for the owning task.

2. Define the exact schema impact.
   - new table
   - new column
   - new foreign key or index
   - data-shape constraint change
   - migration-only cleanup

3. Keep the slice narrow.
   - Schema-only PRs should stay in `db/`, tests, and minimal doc updates.
   - Do not pull repository logic or app DB wiring into the same PR unless explicitly requested.

4. Update the ORM models.
   - Reuse `Base`, `UUIDPrimaryKeyMixin`, and `TimestampMixin`.
   - Keep naming explicit and typed.
   - Preserve compatibility unless the task explicitly owns a breaking schema change.

5. Write or update the Alembic migration.
   - Keep the migration explicit and readable.
   - Make downgrade behavior obvious.

6. Add tests.
   - metadata-level test for expected tables or columns
   - migration application test
   - add DB-integration tests only when the task actually needs runtime DB behavior

7. Verify.
   - run targeted `pre-commit`
   - run the metadata and migration tests
   - run a local `alembic upgrade head` against Postgres if the change affects real compatibility concerns

8. Track deferred schema decisions.
   - If a review suggests stronger enums or constraints that need broader vocabulary decisions, add or update a backlog follow-up instead of guessing.

## Current repo conventions

- string UUID primary keys
- UTC timestamps
- JSON columns for structured worker or memory payloads
- explicit Alembic migration files under `db/migrations/versions/`
- follow-up task `T-013` owns persistence enums and constrained value fields

---
name: db-schema
description: Project-specific SQLAlchemy model and Alembic migration conventions for schema changes under db/.
---

# Database schema changes

Follow [AGENTS.md](../../../AGENTS.md) for ownership and required migration-path
coverage. Inspect db/base.py, relevant models, migrations, and nearby tests first.

- Keep schema/migrations in db/; schema-only work does not absorb repository or
  application wiring.
- Use Base, UUIDPrimaryKeyMixin, and TimestampMixin from db/base.py.
- Keep explicit upgrade/downgrade behavior and compatibility assumptions
  reviewable; do not rely on hidden autogeneration decisions.
- Treat persisted status/enum changes as a current behavior contract: inspect
  writers/readers and the active issue rather than routing to a historical task ID.
- Add metadata-level coverage and migration upgrade plus write assertions;
  exercise app/repository state behavior when affected.

Start with the affected tests and changed-file hooks:

```bash
.venv/bin/pytest tests/unit/test_db_models.py tests/integration/test_db_migrations.py
.venv/bin/pre-commit run --files db/models.py
```

Include any affected migration-specific integration tests. For local Postgres
migration application, use the Compose migrate service or the API container;
never rely on an implicit host DATABASE_URL, which can create a stray SQLite DB:

```bash
docker compose up migrate
docker compose run --rm api alembic upgrade head
```

These apply migrations to the selected stack; confirm that it is the intended
local/test target and follow the task's authorization before changing its data.

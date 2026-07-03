# M23 Slice 2: Full-Text Search Retrieval

> **Prerequisite**: Slice 1 must be merged first. This slice builds on the
> wired-up `load_memory` and `persist_memory` nodes.

## Goal

Add Postgres full-text search so `load_memory` selects relevant memories for a
task instead of loading every personal and project entry. Provide dashboard
search and task-level memory retrieval visibility for inspectability.

## Context

After Slice 1, the graph loads ALL personal and project memories for the
user/repo. This works for small volumes but does not prioritize relevance. The
roadmap says:

> *"consider pgvector only if metrics justify the new infrastructure dependency"*
> *"do not add vector storage just because it is available; add it only when
> measured retrieval quality needs it"*

Full-text search (`tsvector` + GIN) uses existing Postgres capabilities and
provides a measurable retrieval baseline without adding pgvector.

## Design Decisions (resolved)

| Decision | Choice |
|---|---|
| Retrieval approach | Postgres full-text search (`tsvector` + GIN), not pgvector |
| Search index content | `memory_key` + JSON-serialized `value` combined into a generated `tsvector` column |
| SQLite fallback | Empty query returns `[]`; non-empty query falls back to Slice 1 load-all behavior |
| ORM mapping | Do not map `TSVECTOR` in SQLAlchemy models; it breaks SQLite metadata creation |
| Search query source | Task text (`state.task.task_text` or `state.task_spec.goal`) |
| Search scope | Full-text search for BOTH personal and project memories |
| Result limit | Top 20 per category, ranked by `ts_rank`, configurable |
| Search logic location | `search` methods on existing `PersonalMemoryRepository` and `ProjectMemoryRepository` |
| Search implementation | Postgres-only raw SQL/text queries against `search_vector`; ORM loads mapped rows by selected IDs |
| Dashboard changes | Search box on KnowledgeBasePage + memory visibility in task detail timeline |
| Migration | Single Alembic migration with Postgres dialect guard (no-op on SQLite) |

---

## Implementation Steps

### 1. Alembic migration - tsvector column + GIN index

**File**: `db/migrations/versions/<new_revision>.py`

Add a generated `search_vector` column and GIN index to both memory tables:

```python
"""Add full-text search columns to memory tables."""

from alembic import op

revision = "<auto>"
down_revision = "<current_head>"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("""
        ALTER TABLE memory_personal
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(memory_key, '') || ' ' ||
                coalesce(value::text, '')
            )
        ) STORED
    """)
    op.execute("""
        CREATE INDEX ix_memory_personal_search_vector
        ON memory_personal USING GIN (search_vector)
    """)

    op.execute("""
        ALTER TABLE memory_project
        ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('english',
                coalesce(memory_key, '') || ' ' ||
                coalesce(value::text, '')
            )
        ) STORED
    """)
    op.execute("""
        CREATE INDEX ix_memory_project_search_vector
        ON memory_project USING GIN (search_vector)
    """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_index("ix_memory_project_search_vector", table_name="memory_project")
    op.execute("ALTER TABLE memory_project DROP COLUMN search_vector")
    op.drop_index("ix_memory_personal_search_vector", table_name="memory_personal")
    op.execute("ALTER TABLE memory_personal DROP COLUMN search_vector")
```

Key details:

- Generated columns auto-populate for existing rows.
- `value::text` casts the JSON blob to text for indexing.
- SQLite migration is a no-op.
- Do not import `sqlalchemy.dialects.postgresql.TSVECTOR` into `db/models.py`.

### 2. Keep SQLAlchemy models SQLite-compatible

**File**: `db/models.py`

Do not add `search_vector` as an ORM column on `PersonalMemory` or
`ProjectMemory`.

Reason: a direct `TSVECTOR` column mapping prevents SQLite
`Base.metadata.create_all()` from compiling, even though the migration itself is
guarded. The repository search methods should use raw SQL for the Postgres
`search_vector` expression and then load mapped rows by ID.

### 3. Add repository `search` methods

**File**: `repositories/sqlalchemy_memory.py`

#### PersonalMemoryRepository.search

```python
def search(
    self,
    *,
    query: str,
    limit: int = 20,
) -> list[PersonalMemory]:
    """Full-text search on operator-global personal memories."""
    if not query or not query.strip():
        return []

    dialect = self.session.bind.dialect.name if self.session.bind else ""
    if dialect != "postgresql":
        return self.list_all(limit=limit, offset=0)

    normalized_limit = max(1, min(limit, 100))
    id_rows = self.session.execute(
        text("""
            SELECT id
            FROM memory_personal
            WHERE search_vector @@ plainto_tsquery('english', :query)
            ORDER BY
              ts_rank(search_vector, plainto_tsquery('english', :query)) DESC,
              created_at DESC,
              id DESC
            LIMIT :limit
        """),
        {"query": query, "limit": normalized_limit},
    ).scalars().all()

    if not id_rows:
        return []

    rows = self.session.scalars(
        select(PersonalMemory).where(PersonalMemory.id.in_(id_rows))
    ).all()
    by_id = {row.id: row for row in rows}
    return [by_id[memory_id] for memory_id in id_rows if memory_id in by_id]
```

#### ProjectMemoryRepository.search

Use the same pattern, filtering by `repo_url` and selecting from
`memory_project`.

Key details:

- Do not reference `PersonalMemory.search_vector` or
  `ProjectMemory.search_vector`; those ORM attributes do not exist.
- Empty/whitespace-only query returns `[]`.
- Non-Postgres dialects fall back to `list_all` / `list_by_repo` only for
  non-empty queries.
- `plainto_tsquery` safely parses user input without special query syntax.
- The API caps `limit` at 100; the repository should defensively cap it too.

### 4. Update `load_memory` to use search

**File**: `orchestrator/graph.py`

Modify `build_load_memory_node` from Slice 1 to call `repo.search(...)` instead
of `repo.list_all(...)` / `repo.list_by_repo(...)`:

```python
search_query = (
    state.task_spec.goal if state.task_spec and state.task_spec.goal
    else state.task.task_text
)
search_limit = 20

repo = PersonalMemoryRepository(db_session)
for row in repo.search(query=search_query, limit=search_limit):
    personal_entries.append(MemoryEntry(...))

if repo_url:
    repo = ProjectMemoryRepository(db_session)
    for row in repo.search(repo_url=repo_url, query=search_query, limit=search_limit):
        project_entries.append(MemoryEntry(...))
```

- Session state loading remains unchanged and always loads full session state.
- Continue returning `memory.model_dump(mode="json")`.
- Include search metadata in the `MEMORY_LOADED` timeline payload:

```python
payload={
    "retrieval_mode": "full_text",
    "search_query": search_query[:200],
    "search_limit": search_limit,
    "personal_count": len(personal_entries),
    "project_count": len(project_entries),
    "personal_keys": [entry.memory_key for entry in personal_entries],
    "project_keys": [entry.memory_key for entry in project_entries],
}
```

### 5. Add search API endpoints

**File**: `apps/api/routes/knowledge_base.py`

```python
@router.get("/personal/search", response_model=list[PersonalMemorySnapshot])
def search_personal_memory(
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[PersonalMemorySnapshot]:
    ...


@router.get("/project/search", response_model=list[ProjectMemorySnapshot])
def search_project_memory(
    repo_url: str = Query(min_length=1),
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[ProjectMemorySnapshot]:
    ...
```

Wire through `TaskExecutionService` to call the repository `search(...)`
methods. Empty `q` returns `[]`.

### 6. Add dashboard API client methods

**File**: `dashboard/src/services/api.ts`

```typescript
searchPersonalMemory(query: string, limit?: number): Promise<PersonalMemorySnapshot[]>
searchProjectMemory(repoUrl: string, query: string, limit?: number): Promise<ProjectMemorySnapshot[]>
```

### 7. Add search UI to KnowledgeBasePage

**File**: `dashboard/src/components/KnowledgeBasePage.tsx`

- Add search input controls above the personal and project memory lists.
- Debounce query input by 300ms.
- In browse mode, keep the existing paginated list behavior.
- In search mode, call the new search endpoints and show returned entries.
- Provide a clear reset action that returns to browse mode.
- Do not display ranking scores; the API returns snapshots only.

### 8. Show loaded memories in task detail timeline

**File**: task detail / timeline component in dashboard

- Parse `MEMORY_LOADED` timeline event payload.
- Display retrieval mode, search query, result counts, and loaded memory keys
  when present.
- Keep the raw JSON payload available as it is today.

---

## Tests

### Unit tests

#### [NEW] `tests/unit/test_memory_search.py`

| Test | Description |
|---|---|
| `test_search_empty_query_returns_empty` | Empty/whitespace query -> empty list |
| `test_search_sqlite_fallback_for_non_empty_query` | SQLite dialect + non-empty query -> load-all fallback |
| `test_search_respects_limit_cap` | Repository defensively caps large limits |
| `test_search_queries_operator_global_personal_memory` | Personal search is operator-global |
| `test_search_filters_by_repo_url` | Project search scopes by repo |
| `test_models_create_all_on_sqlite_without_tsvector_mapping` | `Base.metadata.create_all()` works on SQLite |

Postgres ranking tests should live in integration coverage because SQLite does
not support `tsvector`.

#### [MODIFY] `tests/unit/test_load_memory_node.py`

- Test that `load_memory` uses repository `search(...)`.
- Test query extraction from `task_spec.goal` vs `task.task_text`.
- Test `MEMORY_LOADED` timeline payload includes retrieval metadata and keys.

### Integration tests

#### [MODIFY] `tests/integration/test_repositories_memory.py`

- Add Postgres-only search tests, skipped when the dialect is not Postgres.
- Test memory_key matches.
- Test JSON value text matches.
- Test ranked ordering with multiple entries.
- Test generated `search_vector` is populated after upsert.

#### [MODIFY] `tests/integration/test_knowledge_base_endpoints.py`

- Test `GET /knowledge-base/personal/search?q=`.
- Test `GET /knowledge-base/project/search?repo_url=&q=`.
- Test empty query handling.
- Test `limit` validation and result limiting.

### Dashboard tests

#### [MODIFY] `dashboard/src/services/api.test.ts`

- Test personal search URL encoding.
- Test project search URL encoding.
- Test empty-array fallback for malformed API responses.

#### [MODIFY] `dashboard/src/components/KnowledgeBasePage.test.tsx`

- Test search controls render.
- Test debounced search calls the API.
- Test search results display.
- Test clearing search returns to browse mode.

#### [MODIFY] `dashboard/src/components/TaskDetailPanel.test.tsx`

- Test `MEMORY_LOADED` timeline payload renders retrieval mode, query, counts,
  and memory keys.

---

## Verification

```bash
# Postgres migration path (preferred repo-supported path)
docker compose up migrate

# Alternative: host Alembic only with an explicit Postgres DATABASE_URL
DATABASE_URL=postgresql+psycopg://... .venv/bin/alembic upgrade head

# Targeted unit tests
.venv/bin/pytest tests/unit/test_memory_search.py tests/unit/test_load_memory_node.py -v

# Memory repo integration
.venv/bin/pytest tests/integration/test_repositories_memory.py -v

# API endpoint integration
.venv/bin/pytest tests/integration/test_knowledge_base_endpoints.py -v

# Dashboard coverage gate
npm run test:coverage --prefix dashboard

# Full suites
.venv/bin/pytest --tb=short
.venv/bin/pre-commit run --all-files
```

### Manual verification

1. Add several memory entries via dashboard with varied content.
2. Submit a task via webhook.
3. Check timeline -> `MEMORY_LOADED` shows search query, mode, counts, and keys.
4. Open KnowledgeBasePage -> use search -> verify relevant results appear.
5. Verify SQLite dev/test metadata creation still works without a `TSVECTOR`
   ORM mapping.

---

## Risks

| Risk | Mitigation |
|---|---|
| Full-text search misses relevant memories | Accept for v1; use measured misses to justify pgvector later |
| `value::text` produces noisy tokens | Start with baseline ranking; refine extraction only with evidence |
| `TSVECTOR` mapping breaks SQLite | Do not map `search_vector` in ORM models; use raw SQL after dialect guard |
| SQLite users get no search benefit | Non-empty queries fall back to Slice 1 load-all behavior |
| Search box UX could be confusing | Keep explicit browse/search modes and reset action |

## Acceptance Criteria

- [ ] Alembic migration adds generated `search_vector` + GIN indexes on Postgres.
- [ ] SQLite migrations remain no-op and SQLite metadata creation still works.
- [ ] No `TSVECTOR` ORM mapping is added to `db/models.py`.
- [ ] Repository `search` methods use raw SQL on Postgres and fallback on SQLite.
- [ ] Empty search query returns `[]`.
- [ ] `load_memory` uses search with task text or TaskSpec goal.
- [ ] Timeline event payload includes retrieval metadata and memory keys.
- [ ] Search API endpoints work.
- [ ] Dashboard search box works.
- [ ] Task detail shows loaded-memory retrieval info.
- [ ] New unit + integration + dashboard coverage tests pass.
- [ ] All existing tests pass.

## Future Considerations

If full-text search proves insufficient, measured by retrieval miss rate in the
eval suite, the next step is pgvector embeddings. Keep the repository `search`
interface stable so the implementation can be swapped without changing callers.

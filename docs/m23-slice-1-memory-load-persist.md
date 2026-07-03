# M23 Slice 1: Wire Up Memory Load & Persist

## Goal

Make the orchestrator graph load skeptical memory from the database before
worker dispatch and persist typed worker-produced memory after the run
completes.

Today `load_memory` and `persist_memory` are no-ops. The CRUD layer, API, and
dashboard work, but task execution does not yet read memories into worker
context or write worker-discovered memories back to storage.

## Context

| Component | Current state |
|---|---|
| `load_memory` node | Passthrough - returns `state.memory` unchanged |
| `persist_memory` node | Placeholder - serializes `memory_to_persist` but never writes |
| `MemoryEntry` in state | Stripped to `memory_key` + `value` only |
| `WorkerResult` | No typed worker-produced memory field yet |
| DB repos | Full CRUD works (`PersonalMemoryRepository`, `ProjectMemoryRepository`, `SessionStateRepository`) |
| Dashboard / API | Full CRUD UI works |

## Design Decisions (resolved)

| Decision | Choice |
|---|---|
| Loading strategy | Load ALL entries per user/repo (v1 volumes are small) |
| Worker memory contract | Add typed `WorkerMemoryEntry` and `WorkerResult.memory_to_persist` |
| Persist behavior | Map worker memory to orchestrator `PersistMemoryEntry`, then write through existing repos |
| MemoryEntry metadata | Carry full skepticism fields (`source`, `confidence`, `scope`, `last_verified_at`, `requires_verification`) |
| JSON serialization | Use `model_dump(mode="json")` when memory crosses graph, worker, timeline, or prompt boundaries |
| Context source | `state.session.user_id` + `state.task.repo_url` |
| Session state | Load T-061 compact session state into `MemoryContext.session` |
| DI pattern | Builder functions closing over a DB session factory, matching existing complex-node pattern |
| Error handling | Graceful degradation - log warnings/errors, never crash the graph |

---

## Implementation Steps

### 1. Expand loaded-memory state

**File**: `orchestrator/state.py`

Add skepticism metadata to `MemoryEntry` so workers can weigh trust:

```python
class MemoryEntry(OrchestratorModel):
    """A structured memory record loaded for a task."""

    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
```

- `datetime` is already imported in `orchestrator/state.py`.
- Do not change `MemoryContext`, `PersistMemoryEntry`, or `SessionStateUpdate`.
- Existing `MemoryEntry(memory_key=..., value=...)` construction remains valid.

### 2. Add worker-produced memory contract

**File**: `workers/base.py`

Add a typed worker-layer memory entry and expose it on `WorkerResult`:

```python
class WorkerMemoryEntry(WorkerModel):
    """A memory update produced by a worker for orchestrator persistence."""

    category: Literal["personal", "project"]
    memory_key: str = Field(min_length=1)
    value: dict[str, Any] = Field(default_factory=dict)
    repo_url: str | None = None
    source: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True


class WorkerResult(WorkerModel):
    ...
    memory_to_persist: list[WorkerMemoryEntry] = Field(default_factory=list)
```

Key details:

- Import `datetime` in `workers/base.py`.
- Keep worker memory separate from orchestrator `PersistMemoryEntry` to avoid a
  worker -> orchestrator import dependency.
- For project entries, use `entry.repo_url` when present; otherwise default to
  `state.task.repo_url`. If neither exists, skip that project entry and log.
- Personal entries require `state.session.user_id`; if no user is available,
  skip and log.

### 3. Add `MEMORY_PERSISTED` timeline event and migration

**Files**: `db/enums.py`, `db/migrations/versions/<new_revision>.py`

Add `MEMORY_PERSISTED = "memory_persisted"` alongside `MEMORY_LOADED` in
`TimelineEventType`.

Because timeline events are constrained in the database, also add an Alembic
migration that updates `ck_task_timeline_events_event_type` using the same
pattern as the existing timeline-event migrations:

- `upgrade`: drop the old check constraint and recreate it with
  `memory_persisted` included.
- `downgrade`: delete `task_timeline_events` rows where
  `event_type = 'memory_persisted'`, then recreate the previous check
  constraint.
- Add migration-path coverage that applies the migration and writes a
  `memory_persisted` timeline row.

Changing only `db/enums.py` is not enough; existing databases would reject the
new event during outcome persistence.

### 4. Convert `load_memory` to a DB-backed builder

**File**: `orchestrator/graph.py`

Replace the current passthrough node with `build_load_memory_node(session_factory)`.
The node should:

- read personal memories with `PersonalMemoryRepository.list_by_user(user_id)`;
- read project memories with `ProjectMemoryRepository.list_by_repo(repo_url)`;
- read compact session state with `SessionStateRepository.get(session_id)`;
- map loaded rows into `MemoryEntry` including all skepticism metadata;
- return an empty `MemoryContext` and log a warning if DB reads fail;
- emit `TimelineEventType.MEMORY_LOADED` with loaded counts.

Return memory with JSON-safe serialization:

```python
return {
    "current_step": "load_memory",
    "memory": memory.model_dump(mode="json"),
    "progress_updates": _progress_update(state, "memory context loaded"),
    **_timeline_event(...),
}
```

`mode="json"` is required because `last_verified_at` may be a `datetime`, and
worker prompt rendering calls `json.dumps(...)` on the memory context.

### 5. Map worker memory into orchestrator state

**File**: `orchestrator/graph.py`

After a `WorkerResult` is available and before `persist_memory` runs, map
`result.memory_to_persist` into `state.memory_to_persist`. Implement this in
`summarize_result` or a small helper called from `summarize_result`.

Behavior:

- Preserve any existing `state.memory_to_persist` entries.
- Convert each `WorkerMemoryEntry` into `PersistMemoryEntry`.
- Default project `repo_url` from `state.task.repo_url` when the worker entry
  omits it.
- Skip entries that cannot be safely scoped (personal without user/session,
  project without repo URL), and log enough context to debug the skip.
- Return the mapped list with `model_dump(mode="json")`.

This step is what makes real worker-produced memories reach persistence; tests
that manually seed `state.memory_to_persist` are not sufficient.

### 6. Convert `persist_memory` to a DB-backed builder

**File**: `orchestrator/graph.py`

Replace the placeholder node with `build_persist_memory_node(session_factory)`.
The node should:

- route personal entries to `PersonalMemoryRepository.upsert(...)`;
- route project entries to `ProjectMemoryRepository.upsert(...)`;
- commit once after all writes;
- log and continue on DB errors;
- emit `TimelineEventType.MEMORY_PERSISTED`.

Return JSON-safe memory data:

```python
return {
    "current_step": "persist_memory",
    "memory_to_persist": [
        entry.model_dump(mode="json") for entry in state.memory_to_persist
    ],
    "progress_updates": _progress_update(
        state,
        f"persisted {persisted_count} memory entries",
    ),
    **_timeline_event(
        state,
        TimelineEventType.MEMORY_PERSISTED,
        message=f"Persisted {persisted_count} memory entries.",
        payload={
            "requested_count": len(state.memory_to_persist),
            "persisted_count": persisted_count,
        },
    ),
}
```

### 7. Update graph assembly

**File**: `orchestrator/graph.py`

Add `session_factory: Callable[[], Session] | None = None` to
`build_orchestrator_graph`, `_add_orchestrator_nodes`, and
`_add_orchestrator_complex_nodes`.

Move `load_memory` and `persist_memory` out of the simple-node list. Register
the DB-backed builders when `session_factory` is present, and keep the current
plain functions as the fallback when it is absent:

```python
builder.add_node(
    "load_memory",
    RunnableLambda(build_load_memory_node(session_factory))
    if session_factory
    else RunnableLambda(load_memory),
)

builder.add_node(
    "persist_memory",
    RunnableLambda(build_persist_memory_node(session_factory))
    if session_factory
    else RunnableLambda(persist_memory),
)
```

The fallback preserves existing graph tests that intentionally compile without
database access.

### 8. Pass `session_factory` from `TaskExecutionService`

**File**: `orchestrator/execution.py`

`TaskExecutionService` already stores `self.session_factory`. Pass it through
when lazily building the graph:

```python
self._graph = build_orchestrator_graph(
    worker=self.worker,
    workspace_manager=self.workspace_manager,
    ...,
    session_factory=self.session_factory,
)
```

No new `TaskExecutionService` constructor parameter is needed.

### 9. Update exports only if tests need them

**File**: `orchestrator/__init__.py`

Export `build_load_memory_node` or `build_persist_memory_node` only if tests or
callers need direct imports. Otherwise keep them internal to `orchestrator.graph`.

---

## Tests

### Unit tests

#### [NEW] `tests/unit/test_load_memory_node.py`

| Test | Description |
|---|---|
| `test_loads_personal_memories` | Session with user_id -> personal entries populated |
| `test_loads_project_memories` | Task with repo_url -> project entries populated |
| `test_loads_session_state` | Session present -> session dict populated |
| `test_no_user_id_skips_personal` | No session -> personal list empty, no error |
| `test_no_repo_url_skips_project` | No repo_url -> project list empty, no error |
| `test_no_session_skips_session_state` | No session -> session dict empty |
| `test_db_error_returns_empty_memory` | DB exception -> warning logged, empty context returned |
| `test_skepticism_metadata_preserved` | source/confidence/scope/etc. round-trip from DB model to `MemoryEntry` |
| `test_last_verified_at_is_json_serializable` | Non-null `last_verified_at` is dumped with `mode="json"` |
| `test_timeline_event_emitted` | Result contains `MEMORY_LOADED` timeline event with correct counts |

#### [NEW] `tests/unit/test_worker_memory_contract.py`

| Test | Description |
|---|---|
| `test_worker_result_accepts_memory_to_persist` | `WorkerResult.memory_to_persist` validates typed memory entries |
| `test_worker_memory_rejects_invalid_category` | Invalid categories are rejected |
| `test_worker_memory_confidence_bounds` | Confidence must be between 0 and 1 |

#### [NEW] `tests/unit/test_persist_memory_node.py`

| Test | Description |
|---|---|
| `test_persists_personal_memory` | Entry with category=personal -> upsert called on `PersonalMemoryRepository` |
| `test_persists_project_memory` | Entry with category=project -> upsert called on `ProjectMemoryRepository` |
| `test_routes_by_category` | Mix of personal + project entries -> correct repo gets each |
| `test_skips_personal_without_user_id` | No session -> personal entries skipped |
| `test_skips_project_without_repo_url` | Entry without repo_url and no task repo -> skipped |
| `test_empty_list_is_noop` | No entries -> no DB calls |
| `test_db_error_does_not_crash` | DB exception -> error logged, graph continues |
| `test_timeline_event_emitted` | Result contains `MEMORY_PERSISTED` event with persisted count |
| `test_persisted_entries_are_json_serializable` | Non-null `last_verified_at` uses JSON-safe dumping |

### Integration tests

#### [MODIFY] `tests/integration/test_orchestrator_graph_execution.py`

- Seed personal, project, and session memory through repositories.
- Compile the graph with `session_factory`.
- Use a worker that returns `WorkerResult.memory_to_persist`.
- Verify loaded memories reach `WorkerRequest.memory_context`.
- Verify worker-produced memory is persisted after graph completion.
- Verify `MEMORY_LOADED` and `MEMORY_PERSISTED` timeline events appear.

#### [MODIFY] `tests/integration/test_db_migrations.py`

- Apply the migration that adds `memory_persisted` to the timeline event check.
- Insert a `task_timeline_events` row with `event_type = 'memory_persisted'`.

---

## Verification

```bash
# Targeted unit tests
.venv/bin/pytest tests/unit/test_load_memory_node.py tests/unit/test_persist_memory_node.py tests/unit/test_worker_memory_contract.py -v

# Existing memory tests still pass
.venv/bin/pytest tests/unit/test_memory_skepticism.py -v

# Integration tests
.venv/bin/pytest tests/integration/test_orchestrator_graph_execution.py tests/integration/test_db_migrations.py -v

# Full suite
.venv/bin/pytest --tb=short
.venv/bin/pre-commit run --all-files
```

---

## Risks

| Risk | Mitigation |
|---|---|
| DB session lifecycle mismatch | Use `session_factory()` as context manager, scoped per node call |
| Existing tests break | `session_factory=None` fallback keeps current no-op functions |
| Worker memories never persist | Add typed `WorkerResult.memory_to_persist` and integration coverage from worker result to DB |
| Datetime metadata breaks worker prompt JSON | Use `model_dump(mode="json")` at graph/worker/timeline boundaries |
| Large memory volume bloats prompt | v1 personal-use volumes are small; Slice 2 adds search-based selection |
| Concurrent upsert races | Existing repos already handle `IntegrityError` with `begin_nested()` |

## Acceptance Criteria

- [ ] `WorkerResult` exposes typed `memory_to_persist` entries.
- [ ] Worker-produced memory is mapped into `OrchestratorState.memory_to_persist`.
- [ ] `load_memory` reads personal + project + session state from DB.
- [ ] `persist_memory` writes mapped memory entries to DB.
- [ ] `MemoryEntry` carries full skepticism metadata.
- [ ] `MEMORY_PERSISTED` is added to `db.enums.TimelineEventType` and the DB check constraint.
- [ ] Memory payloads with `last_verified_at` are JSON serializable.
- [ ] Timeline events `MEMORY_LOADED` and `MEMORY_PERSISTED` are emitted.
- [ ] Graph does not crash on DB errors.
- [ ] Tests without `session_factory` still work.
- [ ] New unit + integration tests pass.
- [ ] All existing tests pass.

# Bootstrap File List

Use this file as the source of truth for the first executable scaffold.

The goal is to make Milestone 0 concrete without pulling Milestone 1+ work forward.

## Create In T-001

### Top-level project files

- `README.md`
  Purpose: explain what the service is, current status, and the first local run/test commands.

- `pyproject.toml`
  Purpose: define Python version, dependencies, formatter/linter/test tooling, and project metadata.

### App entry point

- `apps/__init__.py`
  Purpose: mark `apps/` as a Python package.

- `apps/api/__init__.py`
  Purpose: mark the API entry-point package.

- `apps/api/main.py`
  Purpose: expose the minimal FastAPI app object and local startup target.

### Stable package boundaries

- `orchestrator/__init__.py`
  Purpose: reserve the orchestrator package boundary described in `AGENTS.md`.

- `workers/__init__.py`
  Purpose: reserve the worker package boundary.

- `sandbox/__init__.py`
  Purpose: reserve the sandbox package boundary.

- `memory/__init__.py`
  Purpose: reserve the memory package boundary.

- `tools/__init__.py`
  Purpose: reserve the tools package boundary.

- `db/__init__.py`
  Purpose: reserve the DB package boundary.

### Test harness

- `tests/integration/test_app_bootstrap.py`
  Purpose: prove the app imports cleanly and the bootstrap slice is executable.

## Create In T-002

- `docker-compose.yml`
  Purpose: local Postgres and API service wiring.

- `.env.example`
  Purpose: document local environment variables without embedding secrets.

## Create In T-003

- `apps/api/routes/__init__.py`
  Purpose: route package boundary for API endpoints.

- `apps/api/routes/health.py`
  Purpose: implement `/health` and `/ready` without mixing in business logic.

- `tests/integration/test_health_endpoints.py`
  Purpose: verify the health endpoints return success in local development.

## Hold Until Later Milestones

- `db/models.py`
  Milestone: T-010
  Reason: schema belongs to persistence work, not bootstrap.

- `orchestrator/state.py`
  Milestone: T-012
  Reason: typed workflow state should follow the DB and repository layer.

- `orchestrator/graph.py`
  Milestone: T-020
  Reason: the workflow skeleton belongs to orchestrator implementation, not initial packaging.

- `workers/base.py`
  Milestone: T-040
  Reason: the worker contract should be added when the fake worker path is ready.

- `sandbox/docker_runner.py`
  Milestone: T-031
  Reason: sandbox execution should not appear before workspace and Docker decisions are implemented.

## Guardrails

- Do not add business logic to `apps/`.
- Do not add placeholder code that implies DB, worker, or sandbox behavior already exists.
- Do not create files for later milestones just to make the tree look complete.
- Prefer the smallest scaffold that makes the repo executable and testable.

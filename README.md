# code-agent

`code-agent` is a personal coding-agent service focused on safe, inspectable software tasks.

The long-term system is intended to accept tasks from Telegram or HTTP webhooks, restore
session context, route work to coding workers, execute tasks in isolated workspaces, and
return progress plus final results. This repository is currently in the bootstrap stage.

## Current Status

The repo currently contains:
- project guidance in `AGENTS.md`
- agent development rules plus triggerable repo skills in `.agents/`
- architecture and planning docs in `docs/`
- live progress tracking in `docs/status.md`
- a minimal FastAPI bootstrap app for Milestone 0
- local `/health` and `/ready` endpoints for service verification
- a local Docker Compose stack for `api` + `postgres`
- initial SQLAlchemy models and Alembic migration scaffolding for Milestone 1
- an initial repository layer for users, sessions, tasks, runs, and memory
- a typed orchestrator state schema for future workflow execution
- a LangGraph workflow skeleton that runs the happy path with a fake worker result
- SQLite-backed checkpoint persistence helpers for durable LangGraph workflow resume
- sandbox command artifact capture for stdout/stderr logs, changed-file snapshots, and
  diff summaries
- an initial `CodexWorker` that provisions a real workspace, runs a deterministic toy repo
  task in the sandbox, and returns a contract-compliant `WorkerResult`

This slice intentionally does not include:
- app DB wiring
- app-layer real worker dispatch wiring
- worker-result persistence to the DB or reply layer
- Telegram or webhook task handling

## Project Layout

- `.agents/`: development rules, workflows, and repo-specific skills for coding agents
- `apps/`: application entrypoints only
- `orchestrator/`: workflow state and orchestration logic
- `repositories/`: persistence access patterns and CRUD boundaries
- `workers/`: provider-specific coding worker adapters
- `sandbox/`: isolated workspace and command execution
- `memory/`: structured memory persistence and retrieval
- `tools/`: integration wrappers and tool abstractions
- `db/`: schema and migrations
- `tests/`: automated verification

## Local Bootstrap

Create and activate a virtual environment, then install the project with dev dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Start the bootstrap API locally:

```bash
python -m uvicorn apps.api.main:app --reload
```

Run the local container stack:

```bash
cp .env.example .env
# edit .env and replace the example password before first run
docker compose up --build
```

If your environment uses a private or intercepting CA and Docker builds fail with SSL
verification errors, place the CA certificate at `cert.pem` in the repository root before
building. The Docker image will trust that certificate during package installation.

Verify Postgres reachability from the API container:

```bash
docker compose exec api python -c "import socket; socket.create_connection(('postgres', 5432), 5).close(); print('postgres reachable')"
```

Apply the initial schema locally:

```bash
DATABASE_URL="postgresql+psycopg://code_agent:<your-password>@localhost:5432/code_agent" alembic upgrade head
```

Verify the local service:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Run the bootstrap test and linter:

```bash
pytest
pre-commit run --all-files
```

Commit messages are validated with Commitizen, so use Conventional Commits such as:

```text
feat: add docker compose stack
chore: add pre-commit workflow
```

## CI

GitHub Actions run:
- `pre-commit` on every push, including merges to `master`
- `pytest` on every push, including merges to `master`, enforcing 90% branch coverage and uploading `coverage.xml`
- `pip-audit` weekly and on manual dispatch

Protect `master` in GitHub settings to make those checks authoritative:
- require a pull request before merging
- require the `pre-commit` and `pytest` checks to pass
- require branches to be up to date before merging
- block force pushes and branch deletion

The local `no-commit-to-branch` pre-commit hook still blocks direct commits to `main` and
`master`, but branch protection is the server-side control that actually prevents protected
branch bypasses.

## Dependency Security

The repo includes:
- Dependabot updates for Python dependencies and GitHub Actions
- a scheduled `pip-audit` workflow for Python vulnerability checks

## Next Steps

The current implementation targets are:
- architecture checkpoint after `T-041`
- `T-042 Add baseline worker timeout/cancel handling`
- `T-044 Run one real orchestrator-to-worker vertical slice`

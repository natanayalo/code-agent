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
- a FastAPI bootstrap app with a functional `TaskExecutionService` for Milestone 5/7
- a local Docker Compose stack for `api` + `postgres`
- SQLAlchemy models and Alembic migrations for users, sessions, tasks, runs, artifacts, and skeptical memory (Milestone 8)
- a LangGraph orchestrator that executes the full vertical slice (Ingest -> Graph -> Worker -> Sandbox -> DB)
- sandbox command artifact capture and shared audit integration
- a production-class `CodexCliWorker` that provisions a persistent sandbox container, manages shell sessions, and uses a real provider CLI adapter
- an explicit typed tool registry with policy-aware bash tools and budget enforcement
- **Skeptical Memory (Milestone 8)**: structured memory entries with provenance, confidence, and verification metadata
- **Compact Session State (Milestone 8)**: persistent context (goals, decisions, risks) across multiple task iterations

This slice intentionally does not include:
- Telegram or webhook task handle-off
- a multi-user SaaS layer
- a second worker (Claude) for routing validation

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

Enable the real task-submission path locally:

```bash
export DATABASE_URL="postgresql+psycopg://code_agent:<your-password>@localhost:5432/code_agent"
export CODE_AGENT_ENABLE_TASK_SERVICE=1
python -m uvicorn apps.api.main:app --reload
```

When `CODE_AGENT_ENABLE_TASK_SERVICE=1`, the app bootstraps the real `TaskExecutionService`
and routes submitted tasks through `CodexCliWorker`, which shells out to the local `codex`
CLI for bounded turn-by-turn planning. Optional adapter overrides:

```bash
export CODE_AGENT_CODEX_CLI_BIN=/path/to/codex
export CODE_AGENT_CODEX_MODEL=gpt-5.4
export CODE_AGENT_CODEX_PROFILE=default
export CODE_AGENT_CODEX_TIMEOUT_SECONDS=120
```

Run the local container stack:

```bash
cp .env.example .env
# edit .env and replace the example password before first run
docker compose up --build
```

Optional for local worker runs:

```bash
export CODE_AGENT_WORKSPACE_ROOT="$HOME/.code-agent/workspaces"
```

Set `CODE_AGENT_WORKSPACE_ROOT` if you want the Codex worker to keep sandbox workspaces
outside the system temporary directory.

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
- **Milestone 9**: Structured run observability and diagnostic logging (T-043)
- **Milestone 10**: Second worker integration (Claude) and routing improvements (T-070+)
- **Milestone 6**: Telegram and webhook adapter ingress (T-050 to T-053)

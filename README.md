# code-agent

`code-agent` is a personal coding-agent service focused on safe, inspectable software tasks.

The long-term system is intended to accept tasks from Telegram or HTTP webhooks, restore
session context, route work to coding workers, execute tasks in isolated workspaces, and
return progress plus final results. This repository is currently in the bootstrap stage.

## Current Status

The repo currently contains:
- project guidance in `AGENTS.md`
- architecture and planning docs in `docs/`
- a minimal FastAPI bootstrap app for Milestone 0
- local `/health` and `/ready` endpoints for service verification

This slice intentionally does not include:
- database models or migrations
- LangGraph workflow code
- worker implementations
- sandbox execution logic
- Telegram or webhook task handling

## Project Layout

- `apps/`: application entrypoints only
- `orchestrator/`: workflow state and orchestration logic
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
pre-commit install --hook-type pre-commit --hook-type pre-push --hook-type commit-msg
```

Start the bootstrap API locally:

```bash
python -m uvicorn apps.api.main:app --reload
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
- `pre-commit` on pull requests and pushes
- `pytest` on pull requests and pushes

## Next Steps

The next implementation targets after the bootstrap and health slices are:
- `T-002 Add local infrastructure`
- Milestone 1 persistence and state work

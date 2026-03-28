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
pre-commit install
```

Start the bootstrap API locally:

```bash
python -m uvicorn apps.api.main:app --reload
```

Run the bootstrap test and linter:

```bash
pytest
pre-commit run --all-files
```

## Next Steps

The next implementation target is `T-001 Initialize Project Skeleton`, defined in:
- `docs/tasks/t_001_initialize_project_skeleton.md`
- `docs/bootstrap_file_list.md`

# T-001 Initialize Project Skeleton

This document is the canonical source for T-001 scope, acceptance criteria, and implementation boundaries.
Supporting docs may add context, but they must not override this task definition.

## Goal

Turn the repo from planning-only into a minimal executable service skeleton for Milestone 0.

After this task:
- the repository has a real Python project layout
- the API app can be imported and started locally via `uvicorn apps.api.main:app`
- formatter, linter, and test entry points are configured
- later milestones have clear package boundaries to build into

## Scope

Included:
- project tooling in `pyproject.toml`
- top-level package layout aligned with `AGENTS.md`
- minimal FastAPI app entry point with a valid `uvicorn apps.api.main:app` startup target
- a basic test harness and one startup smoke test
- `README.md` updated with bootstrap run/test instructions

Explicitly excluded:
- Docker Compose and Postgres wiring
- health endpoints beyond what is strictly required for app startup
- DB models, migrations, or repository logic
- LangGraph workflow code
- worker implementations
- sandbox execution logic
- Telegram/webhook business flow

## Files Likely To Change

- `README.md`
- `pyproject.toml`
- `apps/__init__.py`
- `apps/api/__init__.py`
- `apps/api/main.py`
- `orchestrator/__init__.py`
- `workers/__init__.py`
- `sandbox/__init__.py`
- `memory/__init__.py`
- `tools/__init__.py`
- `db/__init__.py`
- `tests/integration/test_app_bootstrap.py`

## Implementation Plan

1. Add `pyproject.toml` with the minimum runtime and dev tooling needed for a bootable service skeleton.
2. Create the top-level package directories defined in `AGENTS.md` so later work lands in stable locations.
3. Add a minimal FastAPI app in `apps/api/main.py` with no business logic, no orchestration coupling, and a valid `uvicorn apps.api.main:app` startup target.
4. Add one integration-style smoke test that proves the app object imports cleanly. T-001 does not require a process-level startup test.
5. Replace the placeholder `README.md` with a short project overview and the approved local bootstrap commands.

## Acceptance Criteria

- [ ] `pyproject.toml` exists and defines formatter/linter/test commands used by the repo
- [ ] the package layout matches the folder ownership described in `AGENTS.md`
- [ ] `apps/api/main.py` exposes an importable FastAPI app and a valid `uvicorn apps.api.main:app` startup target
- [ ] one smoke test covers app bootstrap
- [ ] `README.md` explains project purpose, current status, and basic local commands
- [ ] no DB, worker, sandbox, or webhook behavior is implemented yet

## Tests To Add Or Update

- unit: none required for this slice
- integration: `tests/integration/test_app_bootstrap.py`
- e2e: none required for this slice

## Risks

- letting the bootstrap slice absorb Milestone 1 or 2 work
- locking in a packaging layout that conflicts with the repo ownership model
- adding runtime behavior that belongs in later milestones

## Rollback

Remove the scaffold files introduced by this task and revert `README.md` to its prior minimal state.

## Notes For The Coding Worker

- keep this slice thin and executable
- do not introduce database, worker, or sandbox behavior yet
- prefer placeholders and stable interfaces over speculative implementation
- keep imports clean so later milestones can build on the skeleton without rewrites

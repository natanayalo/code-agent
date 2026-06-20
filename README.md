# code-agent

`code-agent` is a local-first personal professional agent platform, specialized in coding, designed to safely execute real work in sandboxed environments with persistent context, approvals, and inspectable results.

## What It Is

`code-agent` accepts coding tasks from API webhooks and Telegram, persists task/session state in Postgres, routes work to a configured worker runtime, executes in isolated sandbox workspaces, and returns progress + final outcomes with artifacts.

The platform is built for one operator-first use case: reliable coding execution with safety controls, not broad consumer chat.

## What Exists Today

- FastAPI ingress for `/tasks`, `/webhook`, and Telegram updates
- API authentication for task-ingress endpoints via shared secret
- durable task/session/run persistence in Postgres
- queue + lease worker runtime split (`api` process and `worker` process)
- LangGraph orchestrator with routing, approval checkpoints, retries, verifier stage, and timeline events
- CLI-driven worker adapters for Codex CLI, Gemini CLI, and OpenRouter-backed runtime
- persistent Docker sandbox workspaces with audit artifacts and retention
- structured skeptical memory + compact session state persistence
- replay and approval-decision task controls via API
- dashboard knowledge-base management for personal/project skeptical memory entries
- operational metrics and lifecycle progress callbacks

## Product Boundaries

This repo intentionally focuses on coding execution infrastructure. It is not currently a multi-tenant SaaS, app-store mobile product, or autonomous self-modifying platform.

## Architecture At A Glance

The platform is organized into clear layers:

- control plane: ingress, orchestration, routing, approvals, persistence
- worker runtime layer: provider-specific coding loops behind a shared worker contract
- sandbox/tool layer: isolated execution, command policy, artifact capture
- memory layer: personal/project/session state with skeptical verification metadata
- operator surfaces: API, Telegram updates, progress callbacks, metrics
- future layer: bounded scout/reflection/autonomy workflows (roadmapped)

Detailed architecture: [`docs/architecture.md`](docs/architecture.md)

## Operator Docs

- runbook and troubleshooting: [`docs/runbook.md`](docs/runbook.md)
- forward plan: [`docs/roadmap.md`](docs/roadmap.md)
- current snapshot/status: [`docs/status.md`](docs/status.md)
- shipped changes: [`CHANGELOG.md`](CHANGELOG.md)

## Repository Layout

- `apps/`: runtime entrypoints and API routes
- `orchestrator/`: workflow graph, execution service, state transitions
- `workers/`: worker contract + runtime-specific adapters
- `sandbox/`: workspace/container lifecycle and sandbox controls
- `memory/`: memory-domain models and retrieval helpers
- `repositories/`: persistence repositories and CRUD boundaries
- `tools/`: tool registry, policy gates, integration wrappers
- `db/`: SQLAlchemy models and Alembic migrations
- `tests/`: unit/integration coverage

## Local Setup

Install dependencies with Poetry:

```bash
poetry install
poetry run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

Run the API only (local dev mode):

```bash
export CODE_AGENT_RUN_API="1"
export CODE_AGENT_RUN_WORKER="0"
export CODE_AGENT_ENABLE_TASK_SERVICE="1"
export CODE_AGENT_ORCHESTRATOR_BRAIN_ENABLED="0"  # optional TaskSpec enrichment hook
export CODE_AGENT_API_SHARED_SECRET="<shared-secret>"
export DATABASE_URL="postgresql+psycopg://code_agent:<password>@localhost:5432/code_agent"
poetry run python -m uvicorn apps.api.main:app --reload
```

Run production-like local stack (`postgres + migrate + api + worker`):

```bash
cp .env.example .env
scripts/up.sh
```

Verify service health:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

### Database Migrations

To apply database migrations to your local Postgres container, use the included Docker Compose service:

```bash
docker compose up migrate
```

Alternatively, to run manual `alembic` commands, use the `api` container:

```bash
docker compose run --rm api alembic upgrade head
```

> [!NOTE]
> Avoid running `alembic` directly on your host machine without setting `DATABASE_URL`. Doing so will fall back to the default SQLite configuration and create accidental `.db` files in the repository.

### Trace observability (Phoenix + OpenInference)

If you use `scripts/up.sh` and set `CODE_AGENT_ENABLE_TRACING=1`, Phoenix is started automatically.

To start Phoenix manually (for example, if the rest of the stack is already running):

```bash
docker compose --profile observability up -d phoenix
```

Enable tracing in `.env`:

```bash
CODE_AGENT_ENABLE_TRACING=1
CODE_AGENT_TRACING_PROJECT=code-agent-local
CODE_AGENT_TRACING_OTLP_ENDPOINT=http://phoenix:6006/v1/traces
```

Then start the stack with `scripts/up.sh` and open Phoenix at
`http://localhost:6006` to inspect LangGraph/orchestrator traces.

## Dashboard / Operator UI

The dashboard is a React-based PWA located in the `dashboard/` directory.

### Setup and Run

```bash
cd dashboard
npm install
npm run dev
```

The dashboard will be available at `http://localhost:3000`.

### Run with Docker Compose

If you are using the full stack via Docker Compose, the dashboard is included. The startup
script supplies local defaults for mounted auth directories before invoking Compose:

```bash
scripts/up.sh
```

The UI will automatically proxy API requests to the `api` service.

### Build for Production

```bash
cd dashboard
npm run build
```

## CLI Worker Auth Bootstrap

Before running real worker tasks with mounted auth directories, ensure the CLIs are installed and in your `PATH`, then run:

```bash
codex login
agy auth login
```

If host CLIs are unavailable, run one-time login through the worker image:

```bash
docker compose run --rm --no-deps worker codex login
docker compose run --rm --no-deps worker agy auth login
```

### Dashboard Authentication

The dashboard uses HttpOnly cookies for session management. To enable it:

1. Set `CODE_AGENT_ALLOWED_ORIGINS` to your dashboard URL (e.g., `http://localhost:3000`).
2. Set `CODE_AGENT_COOKIE_SECURE=1` if running behind an HTTPS proxy.
3. Users log in via the dashboard UI using the same `CODE_AGENT_API_SHARED_SECRET`.

> [!NOTE]
> **Stateless Logout**: The dashboard uses stateless JWTs with a 1-hour expiry. Logging out clears the browser cookie, but the token remains technically valid until it expires.

## Verification Commands

Run the core checks from the repo virtualenv:

```bash
poetry run pytest tests/unit --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories --cov=sandbox --cov=tools --cov=workers --cov-branch --cov-report=term-missing --cov-report=xml --cov-fail-under=80
poetry run pytest tests/integration
poetry run pre-commit run --all-files
# Dashboard checks
cd dashboard && npm run test:run
```

## Changelog

`CHANGELOG.md` is generated from merged pull requests using
[`git-cliff`](https://git-cliff.org/). To update it locally, install the CLI
with Homebrew and run:

```bash
brew install git-cliff
git cliff --output CHANGELOG.md
```

The `changelog` GitHub Actions workflow also runs after merges to `master`. If
the generated changelog changed, it commits only `CHANGELOG.md` back to
`master` using the `CHANGELOG_DEPLOY_KEY` write deploy key. This is the
repository's narrow exception to the normal "no direct commits to `master`"
rule.

Required GitHub setup:

1. Create a dedicated SSH deploy key for changelog automation.
2. Add the public key under repository deploy keys with write access.
3. Store the private key as the `CHANGELOG_DEPLOY_KEY` Actions secret.
4. Add deploy keys to the `master` ruleset bypass list with "Always allow".

The workflow validates that only `CHANGELOG.md` changed before pushing and
normalizes the generated file before committing it. Pure `CHANGELOG.md` pushes
are ignored by the changelog and general CI push workflows to avoid follow-up
no-op or formatting-only runs after the generated commit lands.

## Current Focus

The next phase prioritizes:

1. TaskSpec + human workflow foundation
2. operator UX via a thin local dashboard/PWA
3. stronger worker-mode/profile strategy and runtime leverage

See [`docs/roadmap.md`](docs/roadmap.md) for full milestone plans and sequencing.

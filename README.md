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

Then start the stack (`scripts/up.sh` or `docker compose up`) and open Phoenix at
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

If you are using the full stack via Docker Compose, the dashboard is included:

```bash
docker compose up
```

The UI will automatically proxy API requests to the `api` service.

### Build for Production

```bash
cd dashboard
npm run build
```

## CLI Worker Auth Bootstrap

Before running real worker tasks with mounted auth directories:

```bash
codex login
gemini auth login
```

If host CLIs are unavailable, run one-time login through the worker image:

```bash
docker compose run --rm --no-deps worker codex login
docker compose run --rm --no-deps worker gemini auth login
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

## Current Focus

The next phase prioritizes:

1. TaskSpec + human workflow foundation
2. operator UX via a thin local dashboard/PWA
3. stronger worker-mode/profile strategy and runtime leverage

See [`docs/roadmap.md`](docs/roadmap.md) for full milestone plans and sequencing.

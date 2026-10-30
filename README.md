# code-agent

`code-agent` is a local-first personal professional agent platform, specialized in coding, designed to safely execute real work in sandboxed environments with persistent context, approvals, and inspectable results.

## What It Is

`code-agent` accepts coding tasks from API webhooks and Telegram, persists task/session state in Postgres, routes work to a configured worker runtime, executes in isolated sandbox workspaces, and returns progress + final outcomes with artifacts.

The platform is built for one operator-first use case: reliable coding execution with safety controls, not broad consumer chat.

## What Exists Today

- FastAPI ingress for `/tasks`, `/webhook`, `/github/webhook`, and Telegram updates
- API authentication for task-ingress endpoints via shared secret
- durable task/session/run persistence in Postgres
- split API/Temporal worker runtime with durable command outbox submission
- Temporal workflows with shared routing, approval, memory, bounded completion-loop repair, verification, review, and timeline domain callables
- CLI-driven worker adapters for Codex CLI, Antigravity CLI, and OpenRouter-backed runtime
- persistent Docker sandbox workspaces with audit artifacts and retention
- structured skeptical memory + compact session state persistence
- replay and approval-decision task controls via API
- dashboard knowledge-base management for personal/project skeptical memory entries
- operational metrics and lifecycle progress callbacks
- dependency-aware execution readiness plus outbox, worker, interaction-wait,
  and terminal-reconciliation signals
- dashboard execution status with dependency state, degraded reasons, and safe
  operator recovery guidance
- sequential decomposed-task execution and an opt-in two-node read-only fan-out pilot

## Product Boundaries

### Weekly dependency reviews

The authenticated schedules API creates read-only dependency reviews in Temporal.
Set the same `CODE_AGENT_ALLOWED_REPOS` registry on the API and worker; the worker
checks the key again when each occurrence runs. Supply an explicit IANA timezone.
For example, send a `PUT` to `/schedules/project-dependencies` with:

```json
{"version":1,"purpose":"dependency_review","repo_key":"project","branch":"main","weekdays":["monday"],"time":"09:00","timezone":"Asia/Jerusalem"}
```

New schedules are paused. Use `POST /schedules/{id}/resume`, `/pause`, or
`/trigger` (manual triggers also work while paused). Identical `PUT` retries
are idempotent; changing a specification requires a new ID. `GET /schedules`
and `GET /schedules/{id}` show upcoming actions and recent outcomes;
`GET /schedules/{id}/runs` lists submitted tasks, with `limit` and `offset`.
Task details contain the timeline and artifacts. A previous nonterminal task
causes the next occurrence to be skipped. Temporal skips overlapping submissions
and catches up missed actions only within one hour.

Temporal keeps only the most recent ten schedule actions; their workflow results
also expire under the Temporal namespace's workflow retention policy. Task
history follows the existing task retention policy. To roll back, pause each
schedule and drain or cancel submitted tasks using existing task controls before
reverting the worker. Scheduling controls require the existing API credentials.

This repo intentionally focuses on coding execution infrastructure. It is not currently a multi-tenant SaaS, app-store mobile product, or autonomous self-modifying platform.

## Architecture At A Glance

The current product uses native provider agents for session-local coding
cognition, Temporal for durable lifecycle coordination, Postgres for
operator-facing projections and durable knowledge, and Docker sandboxes for
repository effects and evidence. Deterministic policy, verification, and
independent review remain separate from provider self-report.

Native Codex and Antigravity commands run only in one-shot, hardened,
per-task Docker containers. The trusted Temporal worker retains Docker
authority; executor containers receive only the task workspace, scratch
directories, staged provider auth, and a task-private public-HTTPS proxy.
They never receive the Docker socket, control-plane credentials, or host auth
mounts. Real-worker validation has exercised this boundary with paired Codex
and Antigravity runs, establishing the expected safe memory behavior without
making a general timing-improvement claim.

The platform is organized into these layers:

- control plane: ingress, orchestration, routing, approvals, persistence
- worker runtime layer: provider-specific coding loops behind a shared worker contract
- sandbox/tool layer: isolated execution, command policy, artifact capture
- memory layer: personal/project/session state with skeptical verification metadata
- operator surfaces: API, Telegram updates, progress callbacks, metrics
- controlled proposal lane: bounded scout/reflection proposals with operator review

Detailed architecture: [`docs/architecture.md`](docs/architecture.md)

## Operator Docs

- runbook and troubleshooting: [`docs/runbook.md`](docs/runbook.md)
- Temporal compatibility baseline and workflow-evolution procedure: [`docs/temporal_compatibility.md`](docs/temporal_compatibility.md)
- forward plan: [`docs/roadmap.md`](docs/roadmap.md)
- current snapshot/status: [`docs/status.md`](docs/status.md)
- Temporal cutover and rollback archive: [`docs/archive/temporal_cutover.md`](docs/archive/temporal_cutover.md)
- shipped changes: [`CHANGELOG.md`](CHANGELOG.md)

## Repository Layout

- `apps/`: runtime entrypoints and API routes
- `orchestrator/`: Temporal workflows, execution service, domain state transitions
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

The Compose stack uses Temporal unconditionally. If Temporal is unavailable,
new submissions fail visibly; there is no runtime selector or polling fallback.

The decomposed-task fan-out pilot is disabled by default. Set
`CODE_AGENT_DECOMPOSED_FANOUT_ENABLED=true` only to enable version-gated,
read-only two-node waves.

Verify service health:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

`/health` reports API-process liveness. `/ready` reports execution readiness
for Postgres, Temporal, a fresh worker-owned command dispatcher, fresh worker
capacity, and deliverable outbox lag. It returns HTTP 503 with stable reason
codes while any global execution blocker is degraded. Authenticated `/metrics`
adds task-specific outbox, interaction-wait, and terminal-reconciliation
signals without turning those individual anomalies into global downtime.

### Readiness alerts

The API can send Telegram messages when a global execution-readiness reason
degrades or recovers. Alerts are disabled by default. Set the existing
`CODE_AGENT_TELEGRAM_BOT_TOKEN` and `CODE_AGENT_READINESS_ALERT_TELEGRAM_CHAT_ID`
(a numeric chat ID) on the API service to enable them; the monitor runs every
30 seconds in the API process and does not depend on the Temporal worker.
Readiness messages include only a stable reason code, transition, and fixed
recovery hint. The same monitor sends task-specific `interaction_wait_stuck`
alerts when a pending interaction is older than the existing 24-hour threshold.
It sends one degraded transition per task and recovers only after a successful
complete-set observation finds no pending interaction for that task past the
threshold. Task alerts use the same opt-in destination and retry/delivery
ordering. Messages include the task ID and fixed safe action, never task text or
private paths; the monitor evaluates the complete affected-task set even though
`/metrics` caps its displayed ID list.

The monitor also sends task-specific `outbox_command_stale` alerts when an
eligible, undelivered Temporal command is more than 60 seconds old. Multiple
commands on one task produce one alert. Recovery requires a newer successful
complete-set observation with no qualifying command for that task. Inspect the
Temporal command outbox and worker dispatcher; the monitor evaluates every
affected task even though `/metrics` caps its displayed task ID list.

It also sends task-specific `terminal_reconciliation_divergent` alerts using
the same 60-second grace period and comparison as authenticated `/metrics`.
The monitor independently reads the complete PostgreSQL projection and
Temporal workflow-status set; it never uses the metrics endpoint's bounded ID
list. Recovery occurs only after a newer successful observation of both stores
confirms that task's divergence is gone. A failed PostgreSQL or Temporal
observation leaves that task's alert state unchanged. The policy includes a
running Temporal workflow without a matching PostgreSQL projection as a
divergence. Before alerting on apparent divergence, the monitor refreshes the
PostgreSQL projection to account for task completion or start acknowledgement
between observations. The monitor defers a still-undelivered start created within the
60-second grace period, or with a dispatch claim that expires within or after
that period. These unconfirmed observations cannot cause degradation or recovery.
A newer observation reports the workflow if it remains unmatched after the
grace period. Metrics retain their existing immediate workflow-only signal.
Alerts are advisory: inspect the task and its Temporal workflow, then use the
existing operator controls if a correction is needed; the monitor does not
alter task or workflow state.

Authenticated `GET /alerts/readiness` shows the latest reason state, and
`GET /alerts/readiness/deliveries` shows recent delivery attempts and failures.
Both endpoints include task-specific `interaction_wait_stuck` records with a
`task_id` field, `outbox_command_stale` records with the same field, and
`terminal_reconciliation_divergent` records with the same field.
Postgres coordinates transition creation and delivery leases across API
instances. Telegram requests are bounded to five seconds, retried at most eight
times with capped exponential backoff, and then retained as failed for operator
inspection. Later transitions for a reason wait until earlier deliveries succeed
or exhaust retries; an exhausted delivery remains visible as failed and does not
block newer transitions. The API claims one message at a time and processes at
most 20 messages per monitor cycle. Delivery history is retained in Postgres
without automatic pruning.
Delivery is at least once: a process crash after Telegram accepts a message but
before its success is recorded can create a duplicate. Postgres outages cannot
be notified until persistence returns, so an independent external monitor is
still needed for immediate database-outage alerting. The API retains at most
100 distinct unpersisted readiness snapshots in memory; restarting the API
during a Postgres outage loses those observations. To roll back, unset the chat
ID and restart the API; persisted status remains available, and task scheduling
and execution are unchanged.
Buffered samples retain their observation time and replay in order in one
Postgres transaction. If replay fails, none of that batch commits and the API
retains it for retry. A previously unreported degradation is retained if a
newer healthy baseline was stored first; distinct outages replay as individual
degraded/recovered pairs, and duplicate stale observations are ignored.
If a later incident was reported before replay, an earlier buffered incident's
notifications arrive afterward.

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
`http://localhost:6006` to inspect Temporal activity and orchestrator-domain traces.

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

Before running real worker tasks with mounted auth directories, ensure the CLIs are installed and in your `PATH`, then authenticate on the host against the directory configured for the worker:

```bash
codex login
GEMINI_HOME="$CODE_AGENT_ANTIGRAVITY_AUTH_DIR" agy
```

For Antigravity, run the trusted operator enrollment container instead. It has
no task workspace, Docker socket, or control-plane credentials; only the
configured provider-auth directory is writable. It uses the same private HTTPS
proxy pattern as the executor:

```bash
CODE_AGENT_ANTIGRAVITY_AUTH_DIR="/absolute/provider-auth-path" \
  scripts/bootstrap_antigravity_auth.sh

# Verify the enrolled credential without exposing a task workspace.
CODE_AGENT_ANTIGRAVITY_AUTH_DIR="/absolute/provider-auth-path" \
  scripts/bootstrap_antigravity_auth.sh --check

```

Complete the browser or printed-URL sign-in, then exit the CLI. Do not
authenticate from the worker or a task executor: the executor home is deleted
after each task and does not receive the host keyring.

If the Codex host CLI is unavailable, run its one-time login through the worker image:

```bash
docker compose run --rm --no-deps worker codex login
```

### Dashboard Authentication

The dashboard uses HttpOnly cookies for session management. To enable it:

1. Set `CODE_AGENT_ALLOWED_ORIGINS` to your dashboard URL (e.g., `http://localhost:3000`).
2. Set `CODE_AGENT_COOKIE_SECURE=1` if running behind an HTTPS proxy.
3. Users log in via the dashboard UI using the same `CODE_AGENT_API_SHARED_SECRET`.

> [!NOTE]
> **Stateless Logout**: The dashboard uses stateless JWTs with a 1-hour expiry. Logging out clears the browser cookie, but the token remains technically valid until it expires.

## Native Agent Event Capture (Milestone M28.5C)

Milestone M28.5C introduces a typed, provider-neutral `AgentEvent` foundation for native agent runs (Codex and Antigravity).

- **Typed Event Stream**: 11 canonical event variants (`agent_started`, `agent_progress`, `agent_message`, `tool_requested`, `tool_completed`, `file_changed`, `permission_requested`, `artifact_produced`, `budget_updated`, `agent_failed`, `agent_completed`).
- **Privacy & Safety**: Provider reasoning and internal chain of thought (`reasoning` in Codex, `thinking` in Antigravity) are suppressed from emitted events. Text fields are redacted with `SecretRedactor` and bounded in length.
- **Artifact Generation**: When event normalization yields events, an `agent-events-v1.jsonl` artifact (artifact name `agent_event_stream`) is created in the run artifact directory.
- **Opt-in Only**: Event capture is disabled by default and does not affect the existing `WorkerResult` contract or persistence layer. Enable via:
  ```bash
  CODE_AGENT_NATIVE_EVENT_CAPTURE_ENABLED=1
  ```
  *(Legacy key `CODE_AGENT_NATIVE_AGENT_EVENT_CAPTURE_ENABLED` is also accepted during rollout migration).*

## Verification Commands

The repository coverage target is 90%. During post-Temporal coverage recovery,
Python CI enforces a 90% coverage floor. Run the currently enforced checks from
the repository virtualenv:

```bash
poetry run pytest -n 3 tests/unit tests/workers --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories --cov=sandbox --cov=tools --cov=workers --cov-branch --cov-report=term-missing --cov-report=xml
poetry run pytest tests/integration
poetry run pre-commit run --all-files
# Dashboard checks
cd dashboard && npm run test:coverage
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

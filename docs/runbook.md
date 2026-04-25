# Runbook

## Purpose

This runbook describes how to boot, operate, debug, and recover the current `code-agent` runtime.

## 1) Worker CLI Auth Bootstrap

Worker containers rely on login-based host auth directories that are mounted into the worker runtime.

Expected mounts:

- `${CODE_AGENT_CODEX_AUTH_DIR}` -> `/root/.codex` (required)
- `${CODE_AGENT_GEMINI_AUTH_DIR}` -> `/root/.gemini` (optional unless Gemini worker is used)

Bootstrap on host:

```bash
codex login
gemini auth login
```

Fallback bootstrap via container:

```bash
docker compose run --rm --no-deps worker codex login
docker compose run --rm --no-deps worker gemini auth login
```

## 2) Process Model

The system runs as split runtimes:

- API process (`CODE_AGENT_RUN_API=1`, `CODE_AGENT_RUN_WORKER=0`)
- Worker process (`CODE_AGENT_RUN_API=0`, `CODE_AGENT_RUN_WORKER=1`)

Both can share the same DB/task service configuration while performing different responsibilities.

Typical local production-like startup:

```bash
cp .env.example .env
scripts/up.sh
```

## 3) Queue + Lease Behavior

Queue lifecycle:

1. API persists a pending task.
2. Worker polls for claimable tasks.
3. Claim sets `lease_owner` and `lease_expires_at`.
4. Heartbeat extends lease during execution.
5. Completion/failure clears lease and persists outcome.
6. Failure may requeue until `max_attempts` is reached.

Relevant environment controls:

- `CODE_AGENT_QUEUE_POLL_INTERVAL_SECONDS` (default `2`)
- `CODE_AGENT_QUEUE_LEASE_SECONDS` (default `60`)
- `CODE_AGENT_QUEUE_MAX_ATTEMPTS` (default `3`)

## 4) Approval Flow

Manual approval checkpoints are persisted in task constraints and surfaced through:

- `POST /tasks/{task_id}/approval` with `{ "approved": true|false }`

Behavior:

- `approved=true`: task returns to `pending` and can be reclaimed
- `approved=false`: task becomes terminal `failed`

## 5) Operator Endpoints

Core endpoints:

- `POST /tasks` submit work
- `GET /tasks/{task_id}` inspect status and latest run
- `POST /tasks/{task_id}/approval` apply manual approval decision
- `POST /tasks/{task_id}/replay` replay terminal task with optional overrides
- `GET /health`, `GET /ready`, `GET /metrics`

Ingress protection:

- `/tasks` and `/webhook` require shared-secret auth (`CODE_AGENT_API_SHARED_SECRET`)

## 6) Common Failure Debugging

## API will not start

Checks:

- verify `CODE_AGENT_RUN_API=1`
- verify DB env vars are present and reachable
- if task service is enabled, verify `CODE_AGENT_API_SHARED_SECRET` is set

Useful command:

```bash
curl http://127.0.0.1:8000/health
```

## Worker idle with queued tasks

Checks:

- verify worker process is running with `CODE_AGENT_RUN_WORKER=1`
- verify `CODE_AGENT_ENABLE_TASK_SERVICE=1`
- verify worker and API share the same database
- inspect lease fields (`lease_owner`, `lease_expires_at`) for stuck claims

Useful command:

```bash
docker compose logs -f worker
```

## Sandbox/container execution failures

Checks:

- verify Docker socket mount exists in worker container
- verify `CODE_AGENT_SANDBOX_IMAGE` is available locally
- verify workspace root mount is valid (`CODE_AGENT_WORKSPACE_ROOT`)

Useful commands:

```bash
docker compose ps
docker images | rg code-agent-worker
```

## CLI auth failures inside worker

Checks:

- ensure auth dirs are mounted and non-empty
- repeat login commands and restart worker process

## Callback delivery rejections

Checks:

- callback URL must be public `http(s)`
- loopback/private/reserved/link-local targets are intentionally blocked by SSRF policy

## 7) Restart + Recovery Patterns

## Normal restart (non-destructive)

```bash
docker compose restart api worker
```

## Full local stack reset (without deleting Postgres volume)

```bash
docker compose down
docker compose up -d
```

## Worker stuck / lease drift recovery

- restart worker process first
- allow expired leases to become claimable again
- avoid direct DB mutation unless absolutely necessary

## Re-run work safely

Use replay endpoint instead of manually cloning task rows:

- `POST /tasks/{task_id}/replay`

## 8) Safety Boundaries (Do Not Bypass)

- do not run task execution directly on host without sandbox boundaries
- do not disable task-ingress auth for shared environments
- do not bypass approval flow for destructive tasks
- do not relax callback SSRF guardrails for convenience
- do not alter secrets/auth/billing/sandbox policy without explicit approval

## 9) Minimal Operational Checklist

Before running tasks:

1. DB reachable and migrations current
2. API and worker runtimes configured correctly
3. CLI auth mounted and valid
4. sandbox image available
5. shared-secret auth configured

After incidents:

1. capture task/run IDs
2. collect worker logs and artifacts
3. classify failure (ingress, queue, worker runtime, sandbox, approval)
4. replay only after root-cause hypothesis is documented

# Runbook

## Purpose

This runbook describes how to boot, operate, debug, and recover the current `code-agent` runtime.

## 1) Worker CLI Auth Bootstrap

Worker containers rely on login-based host auth directories that are mounted into the worker runtime. Note that Antigravity (`agy`) auth uses operating-system secure keyrings (Keychain, DBus Secret Service, etc.), so the auth directory mount (`CODE_AGENT_ANTIGRAVITY_AUTH_DIR`) does not simply copy a plain text token file.

Expected mounts:

- `${CODE_AGENT_CODEX_AUTH_DIR}` -> `/root/.codex` (required)
- `${CODE_AGENT_ANTIGRAVITY_AUTH_DIR}` -> `/root/.gemini` (optional unless Antigravity worker is used)

Bootstrap on host (ensure CLIs are installed and in PATH):

```bash
codex login
agy auth login
```

Fallback bootstrap via container (may not work for Antigravity depending on host OS keyring integration):

```bash
docker compose run --rm --no-deps worker codex login
docker compose run --rm --no-deps worker agy auth login
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

## 2.1) Codex/Gemini Runtime Mode Deprecation Controls

Codex and Gemini defaults are pinned to native-agent execution.

- `CODE_AGENT_CODEX_RUNTIME_MODE` and `CODE_AGENT_GEMINI_RUNTIME_MODE` support
  `native_agent|tool_loop`, but `tool_loop` is deprecated for defaults and is ignored (with a
  startup warning).
- To keep legacy compatibility, explicitly enable legacy profiles:
  - `CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED=1`
  - `CODE_AGENT_GEMINI_TOOL_LOOP_LEGACY_ENABLED=1`
- Legacy profiles should be selected per task using top-level `worker_profile_override`
  (for `/tasks` or `/webhook` payloads), for example `codex-tool-loop-executor`.
- `/metrics` exposes `runtime_mode_usage` and `legacy_tool_loop_usage` so operators can track
  migration progress away from legacy tool-loop execution.

## 2.2) Reflection Proposal Scoring Controls

- `CODE_AGENT_IMPROVEMENT_LLM_SCORING_ENABLED=1` allows configured planner workers to revise
  reflection improvement proposal scoring fields and attach model rationale.
- Deterministic scoring remains the fallback when model scoring is disabled, unavailable, invalid,
  or timed out. Proposal metadata records the scoring mode, provider, rationale, and fallback reason.

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

## 3.1) Tracing and Observability (Phoenix OSS)

`code-agent` can emit OpenTelemetry/OpenInference traces for LangGraph/orchestrator runs.

Manual operations:

- [tracing_manual.md](tracing_manual.md)

Enable tracing env vars:

- `CODE_AGENT_ENABLE_TRACING=1`
- `CODE_AGENT_TRACING_PROJECT=<project-name>`
- `CODE_AGENT_TRACING_OTLP_ENDPOINT=http://phoenix:6006/v1/traces`

Run local/self-hosted Phoenix:

```bash
docker compose --profile observability up -d phoenix
```

Note:

- `scripts/up.sh` starts `phoenix` automatically when `CODE_AGENT_ENABLE_TRACING=1`.
- Use the command above when the stack is already up and you only need to add observability.

Phoenix UI and OTLP endpoints:

- UI: `http://localhost:6006`
- OTLP HTTP collector: `http://localhost:6006/v1/traces`
- OTLP gRPC collector: `localhost:4317`

## 4) Approval Flow

Manual approval checkpoints are persisted in task constraints and surfaced through:

- `POST /tasks/{task_id}/approval` with `{ "approved": true|false }`

Behavior:

- `approved=true`: task returns to `pending` and can be reclaimed
- `approved=false`: task becomes terminal `failed`

## 5) Operator Endpoints

Current Operator UI:

- **Dashboard**: `http://localhost:3000` (started automatically with `scripts/up.sh`)

Core API endpoints:

- `POST /tasks` submit work
- `GET /tasks/{task_id}` inspect status and latest run
- `POST /tasks/{task_id}/approval` apply manual approval decision
- `POST /tasks/{task_id}/cancel` cancel a running or pending task
- `POST /tasks/{task_id}/interactions/{interaction_id}/response` resume after clarifying/fixing interaction
- `POST /tasks/{task_id}/replay` replay terminal task with optional overrides
- `GET /health`, `GET /ready`, `GET /metrics`

Ingress protection:

- `/tasks` and `/webhook` require shared-secret auth (`CODE_AGENT_API_SHARED_SECRET`)
- webhook operation manual: [webhook_manual.md](webhook_manual.md)

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
docker images "${CODE_AGENT_SANDBOX_IMAGE:-code-agent-worker}"
```

## CLI auth failures inside worker

Checks:

- ensure auth dirs are mounted and non-empty
- repeat login commands and restart worker process

### Antigravity (`agy`) specific issues

**`agy: command not found`**
Ensure that the Antigravity CLI is installed and its binary is available in the `PATH` environment variable of the context executing the command (host or Docker worker).

**Locked Keyrings or DBus Errors**
Antigravity stores auth tokens in OS keyrings. In a headless environment (like Linux Docker containers), you might see DBus errors or locked keyrings. Ensure a compatible Secret Service is running or fallback auth mechanisms are configured correctly per official Antigravity documentation.

**Permission-prompt timeouts**
If `agy` runs hang and eventually timeout, it might be prompting for user permission interactively. Verify that `CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION` is set to a non-interactive mode (e.g. `proceed-in-sandbox`) and that settings are propagated correctly.

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

## 9) Local E2E Verification

For full pipeline testing (API -> Orchestrator -> Sandbox Worker -> DB), use the automated QA runbook. Ensure your `.env` has test credentials and the stack is running.

```bash
python scripts/run_e2e_qa.py
```

## 10) Antigravity Migration Guide

When migrating existing workspaces and settings to Antigravity:
- **Context Behavior**: Antigravity parses `AGENTS.md` automatically from the workspace. Ensure context instructions are moved there.
- **Legacy Plugin Import**: Any legacy plugins used via Gemini need to be translated or imported into Antigravity's plugin architecture.
- **Skills Path Migration**: Custom skills should be moved into the `.agents/skills/` directory within your workspace.
- **MCP Config Relocation**: Move any MCP configurations into `.agents/` as Antigravity reads configurations from the local workspace settings.

## 11) Minimal Operational Checklist

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

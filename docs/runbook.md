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

## 2.1) Codex and Antigravity Runtime Mode Deprecation

Codex and Antigravity native execution workers are now native-only.

- `CODE_AGENT_CODEX_RUNTIME_MODE`, `CODE_AGENT_GEMINI_RUNTIME_MODE`, and legacy tool-loop configurations (e.g., `CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED`, `CODE_AGENT_GEMINI_TOOL_LOOP_LEGACY_ENABLED`) are deprecated, ignored by the factory, and no longer create operation-selector profiles like `codex-tool-loop-executor` or `gemini-tool-loop-executor`.
- `/metrics` still exposes `runtime_mode_usage` and `legacy_tool_loop_usage` for historical migration tracking.

## 2.2) Reflection Proposal Scoring Controls

- Configured planner workers revise reflection improvement proposal scoring fields and attach model rationale.
- Deterministic scoring remains the fallback when model scoring is unavailable, invalid,
  or timed out. Proposal metadata records the scoring mode, provider, rationale, and fallback reason.

## 3) Temporal Execution Behavior

Temporal is the only task execution runtime. Every submission persists
`orchestration_runtime=temporal` and a transactional start command. The worker
process starts Temporal workflows and activities; it cannot select or fall back
to Postgres task polling.

Set `TEMPORAL_ONLY_CUTOVER_AT` to the accepted cutover timestamp. Historical
legacy/unknown rows remain visible in runtime-drain metrics. If Temporal is
unavailable, new submissions return HTTP 503 while inspection and interaction
endpoints remain available.

Execution lifecycle:

1. API persists a pending task and durable Temporal start command atomically.
2. The worker-owned dispatcher idempotently starts `TaskExecutionWorkflow`.
3. Temporal coordinates approvals, clarification, permission escalation,
   retries, cancellation, and activity recovery.
4. Activities persist worker runs, task projections, timelines, and artifacts.
5. Terminal delivery reconciles the Temporal result with Postgres product state.

Current lifecycle boundaries:

- sequential decomposed tasks are supported
- bounded two-node read-only fan-out is disabled by default and requires
  `CODE_AGENT_DECOMPOSED_FANOUT_ENABLED=true`
- verifier and independent-review repair requests run through a bounded
  retained-workspace completion loop using the selected worker and normal
  permission-escalation path
- repair acceptance repeats verification and independent review; unavailable,
  rejected, ineligible, or exhausted repair ends as `incomplete_delivery` with
  `next_action_hint=await_manual_follow_up`
- deep-scout repo-to-research phase chaining is deferred

Completion-loop rollback boundary:

- workflow histories created before M25.4 replay through the patch-false
  single-pass sequence
- histories that record `m25-4-temporal-completion-loop` require the patch-aware
  workflow code until they close
- a raw code revert is safe only before any patched history exists; afterward,
  deploy a replay-compatible rollback that retains both patch branches

`CODE_AGENT_QUEUE_MAX_ATTEMPTS` remains temporarily as the product-level
logical-attempt policy name. Poll interval, task lease, worker ID/capacity, and
checkpoint settings have been removed.

## 3.1) Tracing and Observability (Phoenix OSS)

`code-agent` can emit OpenTelemetry/OpenInference traces for Temporal activities
and orchestration domain callables.

Manual operations:

- [Tracing skill](../.agents/skills/tracing/SKILL.md)

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

- `approved=true`: the decision is persisted and a Temporal resume signal is queued
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

`/health` is process liveness and remains HTTP 200 while the API can answer.
`/ready` is public execution readiness and returns HTTP 503 when Postgres,
Temporal, the worker-owned dispatcher, fresh worker capacity, or deliverable
outbox progress is unavailable. Its response contains stable reason codes and
safe timestamps, not raw connection errors. Postgres and Temporal probes each
use a two-second operation budget. New submissions retain their own
Temporal availability check and return 503 when Temporal is unavailable;
durable reads and interaction responses remain available.

Authenticated `/metrics` includes an `execution_health` object with command
outbox counts and age, worker/dispatcher freshness, stuck interaction waits,
and Temporal/Postgres terminal reconciliation. Dead letters, stuck waits, and
per-task divergence are degraded operator signals but do not make the entire
API non-ready.

Useful commands:

```bash
curl -i http://127.0.0.1:8000/ready
curl -H "X-Webhook-Token: $CODE_AGENT_API_SHARED_SECRET" \
  http://127.0.0.1:8000/metrics
```

Safe recovery guidance:

| Reason or signal | Safe first action |
| --- | --- |
| `task_service_unconfigured` | Verify API task-service configuration, including `CODE_AGENT_ENABLE_TASK_SERVICE`, then restart the API after correcting its startup settings. |
| `postgres_unavailable` | Restore Postgres connectivity, then wait for `/ready` to recover without restarting the API. |
| `temporal_unavailable` | Restore Temporal and verify its cluster health; submissions remain disabled until the next successful probe. |
| `worker_unavailable` or `dispatcher_unavailable` | Inspect and restart the worker process; do not start worker execution directly on the host. |
| `dispatcher_backlog_stale` | Inspect worker logs and outbox error metrics, then restart the worker if dispatch is not progressing; do not delete outbox rows. |
| `command_retries_present` | Inspect retrying commands and worker logs; allow bounded retries to continue unless progress has stopped. |
| `command_dead_letters_present` | Inspect the affected task and command error, correct the non-retryable cause, and use supported replay/operator controls instead of editing rows. |
| `interaction_wait_stuck` | Answer, reject, or cancel the affected interaction through the dashboard or API. |
| `terminal_state_divergence` | Compare the task timeline with Temporal workflow state, restore the worker if needed, and avoid direct terminal-state updates. |
| `terminal_reconciliation_unknown` | Restore Temporal visibility before treating the divergence count as zero. |

Ingress protection:

- `/tasks` and `/webhook` require shared-secret auth (`CODE_AGENT_API_SHARED_SECRET`)
- webhook operation workflow: [Webhooks skill](../.agents/skills/webhooks/SKILL.md)

## 6) Common Failure Debugging

## API will not start

Checks:

- verify `CODE_AGENT_RUN_API=1`
- verify DB env vars are present and reachable
- if task service is enabled, verify `CODE_AGENT_API_SHARED_SECRET` is set

Useful command:

```bash
curl http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/ready
```

## Worker idle with queued tasks

Checks:

- verify worker process is running with `CODE_AGENT_RUN_WORKER=1`
- verify `CODE_AGENT_ENABLE_TASK_SERVICE=1`
- verify worker and API share the same database
- inspect the Temporal workflow and worker-container status
- inspect pending Temporal command outbox rows if the workflow never started
- inspect `/ready` and authenticated `/metrics` before opening raw logs

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

## Worker or workflow recovery

- Normal container and pod termination sends `SIGTERM`; the worker cancels and
  awaits its Temporal loops, then closes outbound HTTP clients before exiting.
- restart the worker process first; Temporal resumes or retries recorded work
- inspect Temporal UI plus the task timeline and worker-run artifacts
- inspect pending Temporal command outbox rows if a workflow never started
- avoid direct DB mutation unless absolutely necessary

## Schema rollback after M25.3 Slice 4B

An Alembic downgrade recreates the retired lease columns but cannot reconstruct
their dropped values. Use the pre-4B database snapshot for a legacy-image
rollback:

1. stop API and worker services so the application database is quiescent
2. restore the pre-4B snapshot and verify Alembic revision `20260720_0046`
3. deploy `m25.3-legacy-lkg-20260727` API, worker, migrate, and dashboard images
   with their matching configuration
4. start services, verify readiness, and reconcile task/run row counts before
   accepting submissions

The exact image digests, snapshot checksum, rehearsal results, and full restore
procedure are recorded in the
[Temporal cutover archive](archive/temporal_cutover.md).

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
.venv/bin/python .agents/skills/e2e-qa/scripts/run_e2e_qa.py
```

To verify the delivery integration variant, use:

```bash
.venv/bin/python .agents/skills/e2e-qa/scripts/run_e2e_qa_delivery.py
```

### M25.6 Temporal reliability evidence collection

The M25.6 collector is incremental and does not submit tasks, invoke providers,
change routing, or write to Postgres. Submit and poll later real-worker cases
through the existing authenticated E2E API workflow above, then capture each
terminal task immediately so Temporal retention cannot remove its history.

Before deploying the API and workers, start from a clean worktree and set
`BUILD_SHA` to the exact commit being evaluated. Set `CODE_AGENT_ENV` to the
environment label that will be pinned in the bundle. Compose passes both values
to the API and worker runtime manifests. Use the same values when initializing
the bundle; do not use the current checkout SHA unless it is the deployed
build.

```bash
export BUILD_SHA="0123456789abcdef0123456789abcdef01234567"
export CODE_AGENT_ENV="m25-6-local"
export DATABASE_URL="postgresql+psycopg://..."

scripts/up.sh

.venv/bin/python scripts/e2e/run_temporal_reliability_eval.py init \
  --bundle-dir artifacts/m25-6/baseline-01 \
  --environment "$CODE_AGENT_ENV" \
  --operator "operator-name" \
  --database-url-env DATABASE_URL \
  --temporal-address temporal:7233 \
  --temporal-namespace default
```

`init` freezes the checked-in 20-case suite and pins the build SHA,
environment, operator, database environment-variable name, and Temporal
endpoint. A bundle must contain tasks from one deployment and environment only.

After a case reaches its expected terminal state, record the operator-only
annotations and capture it before moving to the next deployment:

```bash
.venv/bin/python scripts/e2e/run_temporal_reliability_eval.py capture \
  --bundle-dir artifacts/m25-6/baseline-01 \
  --case-id mutation-codex-01 \
  --task-id "task-id" \
  --manual-log-inspection no \
  --ci-rejection-count 0 \
  --review-rejection-count 0
```

Use `--next-action` for failed tasks; failures without a typed, non-`unknown`
failure kind and actionable next step fail the evidence gate. A capture is
immutable, and the CLI refuses duplicate case IDs and task IDs. Failed gates
are retained for diagnosis and return a nonzero exit code.

For a bounded repair fixture that must be introduced only after the coding
worker finishes, put ordinary acceptance checks in `verification_commands` and
the fixture setup command in the authenticated operator-only constraint
`operator_post_worker_verification_commands`. The latter is appended only by
deterministic verification and is intentionally omitted from worker prompts.
Do not put secrets in either field: task constraints and private evidence are
still persisted for operator inspection.

To exercise the Antigravity permission-escalation loop without changing the
task's mutation mode, submit the initial task with
`granted_permission=read_only`. The native worker uses strict tool permission,
reports a `workspace_write` request when mutation is denied, and the approved
Temporal retry receives the granted workspace-write boundary.

Generate the aggregate only after captures are current:

```bash
.venv/bin/python scripts/e2e/run_temporal_reliability_eval.py report \
  --bundle-dir artifacts/m25-6/baseline-01 \
  --json-output artifacts/m25-6/baseline-01/report.json \
  --markdown-output artifacts/m25-6/baseline-01/report.md
```

The `artifacts/` tree is gitignored and contains private task evidence and raw
Temporal histories. The generated JSON and Markdown pass through a public-field
allowlist that excludes task text, repository URLs, summaries, responses, logs,
secrets, notes, artifact URIs, and task IDs. Commit a reviewed redacted report
only after all 20 captures pass and the operator confirms it is safe. A
`ready_for_operator_review` result is a technical evidence gate only; it never
resumes M26/M27 or changes production routing automatically.

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
3. classify failure (ingress, outbox/Temporal, worker runtime, sandbox, approval)
4. replay only after root-cause hypothesis is documented

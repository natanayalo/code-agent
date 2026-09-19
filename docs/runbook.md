# Runbook

## Task completion and delivery acceptance

A worker's `success` is execution evidence, not task acceptance. A task requesting
a branch or draft PR completes only after required verification passes and the
broker records matching delivery metadata and a current-attempt
`delivery_completed` event. A worker-supplied PR URL alone is insufficient.
Missing delivery produces `incomplete_delivery` and preserves files, commands,
and artifacts for inspection. Required verifier unavailability produces
`infra_verifier_unavailable`; unrelated advisory warnings remain non-blocking.
Read-only tasks still have to pass their explicitly required verification commands.

Read-only native execution compares source contents and entry metadata immediately
before and after each invocation. Inherited coding-worker edits remain visible in
task diff artifacts but are not attributed to the verifier. Real invocation changes
and unavailable snapshots block verification. Existing runtime exclusions apply;
read-only filesystem mounts and broker Git isolation remain in force.

Environment setup failures retain the identity of an already provisioned workspace
for diagnosis, but block worker dispatch. In particular, a missing required lockfile
does not authorize a non-reproducible installation. Use an explicitly approved
setup command or the existing non-reproducible-install policy when appropriate,
then replay the task. No host-side repository execution is introduced.

## Provider pre-dispatch diagnostics

Before a worker is launched, the orchestrator evaluates the selected concrete
worker's credential contract and runtime prerequisites. Container-backed
workers must have a responsive Docker daemon and an available executor image;
an unready prerequisite blocks launch and persists a diagnostic artifact with
the task attempt. OpenRouter uses its configured HTTP API client and does not
require a provider CLI binary.

The CLI-binary check is intentionally scoped to what the host can verify. For
container execution it records the configured executable as **unverified**
inside the image and remains non-blocking; it does not guarantee that a binary
will be present in the image. Host-native execution checks the executable on
the host `PATH`. The diagnostics CLI loads the configured worker adapters and
container settings directly from environment variables, so it can report a
genuinely ready provider without booting the database-backed task service. Use
it for an operator-visible snapshot:

```bash
.venv/bin/python scripts/check_provider_diagnostics.py \
  --providers codex,antigravity,openrouter \
  --required codex,openrouter
```

The API snapshot remains a limited host-level probe when the API process does
not have access to the configured task-service workers; in that case it fails
required-provider checks closed rather than declaring readiness.

If a task is rejected before dispatch, inspect its persisted
`pre-dispatch-diagnostics.json` artifact and apply the reported remediation
before replaying the task.

Temporal histories use the `task-delivery-acceptance-v1` patch marker to adopt the
delivery activity's terminal outcome. Older histories retain their recorded
workflow-return behavior for replay compatibility; persisted task acceptance uses
the corrected boundary. Roll back by redeploying the preceding reviewed revision;
no database migration is required. Do not remove the patch marker while retained
histories still require it.

## Purpose

This runbook describes how to boot, operate, debug, and recover the current `code-agent` runtime.

## 1) Worker CLI Auth Bootstrap

Worker containers rely on login-based host auth directories that are mounted into the worker runtime. Note that Antigravity (`agy`) auth uses operating-system secure keyrings (Keychain, DBus Secret Service, etc.), so the auth directory mount (`CODE_AGENT_ANTIGRAVITY_AUTH_DIR`) does not simply copy a plain text token file.

Expected mounts:

- `${CODE_AGENT_CODEX_AUTH_DIR}` -> `/root/.codex` (required)
- `${CODE_AGENT_ANTIGRAVITY_AUTH_DIR}` -> `/root/.gemini` (optional unless Antigravity worker is used)

Preflight and native execution share the same auth-directory resolution. Codex
checks `CODE_AGENT_CODEX_AUTH_DIR`, then `CODEX_HOME`, then the user and
container home defaults; OAuth uses the first directory containing `auth.json`.
Antigravity checks `CODE_AGENT_ANTIGRAVITY_AUTH_DIR`, `GEMINI_HOME`,
`CODE_AGENT_GEMINI_AUTH_DIR`, then the user and container home defaults; it
uses the first directory containing the Antigravity OAuth token. If an earlier
directory is configured but lacks the required credential, a later valid
fallback is used by both preflight and execution.

Bootstrap on host (ensure CLIs are installed and in PATH):

```bash
codex login
GEMINI_HOME="$CODE_AGENT_ANTIGRAVITY_AUTH_DIR" agy
```

Antigravity authentication must be enrolled through the trusted, operator-run
container rather than the worker or a task executor. It has a private network
with the HTTPS proxy as its only egress path, mounts no workspace or Docker
socket, and grants write access only to the configured provider-auth directory:

```bash
CODE_AGENT_ANTIGRAVITY_AUTH_DIR="/absolute/provider-auth-path" \
  scripts/bootstrap_antigravity_auth.sh

# Runs a fixed prompt in the same trusted enrollment container. It does not
# mount a task workspace or Docker socket.
CODE_AGENT_ANTIGRAVITY_AUTH_DIR="/absolute/provider-auth-path" \
  scripts/bootstrap_antigravity_auth.sh --check

```

Complete the browser or printed-URL OAuth flow and exit the CLI. The worker
mount remains read-only; every task executor receives a staged copy which is
deleted after artifact collection.

Fallback bootstrap via container is supported for Codex only:

```bash
docker compose run --rm --no-deps worker codex login
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

### Native-agent isolation boundary

Native provider commands are never executed as subprocesses in the long-lived
Temporal worker. Each run uses a one-shot Docker container with a read-only
root filesystem, dropped capabilities, `no-new-privileges`, PID/CPU/memory
limits, private IPC, bounded tmpfs, and only task workspace/artifact/provider
scratch mounts. Provider auth is staged into task scratch and deleted after
artifact collection. The executor has no Docker socket, database, Temporal,
API, or ambient provider credentials. Egress is HTTPS CONNECT through a
task-private proxy; private, loopback, metadata, control-plane, and
DNS-rebinding destinations are rejected. On timeout or cancellation the worker
removes the executor, proxy, and private network while retaining partial
artifacts and the redacted isolation manifest. Roll back by reverting and
redeploying this change; never re-enable host CLI execution. M28 live evidence
remains paused while this prerequisite is under verification.

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

**Default model**
Native Antigravity tasks default to `gemini-3.8-flash` with medium effort. The
currently available model slugs are documented in `.env.example`; model slugs
with an effort suffix can also be passed directly. To override the default,
first run `agy models` through the trusted enrollment container and set
`CODE_AGENT_ANTIGRAVITY_MODEL` to one of the returned IDs. Do not use
legacy `auto-*` values such as `auto-gemini-2.5`: they are routing aliases, not
valid explicit `agy --model` IDs, and are rejected during model resolution.

**`agy: command not found`**
Ensure that the Antigravity CLI is installed and its binary is available in the `PATH` environment variable of the context executing the command (host or Docker worker).

**Locked Keyrings or DBus Errors**
Antigravity stores auth tokens in OS keyrings. In a headless environment (like Linux Docker containers), you might see DBus errors or locked keyrings. Ensure a compatible Secret Service is running or fallback auth mechanisms are configured correctly per official Antigravity documentation.

**Permission-prompt timeouts**
Native print-mode runs use Antigravity's noninteractive permission flag because
its persisted `toolPermission` setting alone can soft-deny repository reads.
This flag is safe only within the Docker-native executor: the task workspace
mount and post-run read-only validation remain the enforcement points.

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
`granted_permission=read_only`. The native worker starts the mutation under
an enforced adapter boundary, reports a `workspace_write` request without
invoking the provider, and the approved Temporal retry runs the real provider
inside the granted workspace-write boundary. Native read-only tasks continue
to use `strict` tool permission.

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

### M28 paired memory-effectiveness baseline

The M28.1 evaluator is deterministic and runs the existing database-backed
memory load path. It never submits a task, invokes a provider, or changes
production memory policy. It writes a local report containing reloaded fixture
metadata and worker-visible context, so use only the checked-in synthetic suite
or a disposable evaluation database.

```bash
.venv/bin/python scripts/e2e/run_memory_effectiveness_eval.py \
  --output artifacts/evaluations/m28-memory-effectiveness-report.json
```

To compare the PostgreSQL full-text path, set a disposable database URL in an
environment variable. The runner applies migrations and writes fixtures, so do
not point it at an operator or production database.

```bash
export CODE_AGENT_M28_EVAL_DATABASE_URL="postgresql+psycopg://..."
.venv/bin/python scripts/e2e/run_memory_effectiveness_eval.py \
  --postgres-url-env CODE_AGENT_M28_EVAL_DATABASE_URL \
  --output artifacts/evaluations/m28-memory-effectiveness-postgres-report.json
```

An exit status of `0` means all four paired assertions passed; `1` means the
report captured a context, gate, or session-continuity regression. This is a
context-delivery baseline only—it does not establish worker outcome improvement
or justify semantic/vector retrieval.

The report's `retrieval_mode` records the search backend actually exercised:
the default SQLite run reports `sqlite_substring_fallback`, while a PostgreSQL
run reports `postgres_full_text`. `timeline_retrieval_mode` preserves the
existing load-memory timeline diagnostic separately.

### M28 real-worker effectiveness collection

The real-worker collector compares a cold task with an assisted repeat for all
four memory scenarios on both native read-only profiles. It submits real tasks
and writes evaluator-owned fixtures, so run it only on a disposable
Postgres/Temporal/sandbox stack and disposable repository.

```bash
.venv/bin/python scripts/e2e/run_m28_real_worker_eval.py init \
  --bundle-dir artifacts/m28-real-worker/baseline-01 \
  --build-sha "$BUILD_SHA" --environment m28-local \
  --repository-revision "$(git rev-parse HEAD)" --operator "operator-name" \
  --ack-disposable-stack

.venv/bin/python scripts/e2e/run_m28_real_worker_eval.py run-batch \
  --bundle-dir artifacts/m28-real-worker/baseline-01 \
  --repo-url <disposable-repository-url>

.venv/bin/python scripts/e2e/run_m28_real_worker_eval.py report \
  --bundle-dir artifacts/m28-real-worker/baseline-01 \
  --json-output artifacts/m28-real-worker/report.json \
  --markdown-output artifacts/m28-real-worker/report.md
```

Keep the private bundle under ignored `artifacts/`: it contains task IDs and
authenticated timeline data. The public report is allowlisted and excludes task
text, repository URLs, memory values, logs, artifacts, secrets, and notes.
An `effective` report after manual private-bundle review was the evidence gate
used to close M28. When reproducing that evaluation, use
`cleanup --repo-url <disposable-repo-url>` to remove only evaluator-owned
fixtures.

### M29 provider reliability advisory report

The M29 provider reliability report is an offline, read-only analysis tool that
evaluates persisted real-task outcomes from PostgreSQL without modifying live
routing, creating database migrations, or updating `evaluation/routing_metrics.json`.

Generate the report using the operator CLI:

```bash
export DATABASE_URL="postgresql+psycopg://..."

# Canonical Current-Cohort Advisory Report (default: current_execution_cohort)
.venv/bin/python scripts/e2e/run_provider_reliability_report.py \
  --database-url-env DATABASE_URL \
  --evidence-scope current_execution_cohort \
  --lookback-days 90 \
  --min-samples 10 \
  --json-output evaluation/m29_provider_reliability_report.json \
  --markdown-output evaluation/m29_provider_reliability_report.md

# Operational Diagnostic Report (all historical tasks & model vintages)
.venv/bin/python scripts/e2e/run_provider_reliability_report.py \
  --database-url-env DATABASE_URL \
  --evidence-scope operational \
  --lookback-days 90 \
  --min-samples 10 \
  --json-output evaluation/m29_provider_reliability_operational_report.json \
  --markdown-output evaluation/m29_provider_reliability_operational_report.md
```

#### CLI options and policy parameters

- `--database-url-env`: Name of the environment variable storing the database
  connection URL. In PostgreSQL environments, the transaction is executed with
  `SET TRANSACTION READ ONLY`.
- `--evidence-scope`: Evidence filtering scope:
  - `current_execution_cohort` (default): Strictly filters tasks to those whose worker runs
    persisted authoritative `model_execution` budget metadata matching the current expected
    cohorts (`codex: gpt-5.6-luna/high`, `antigravity: gemini-3.8-flash/medium`). Zero heuristic
    inference.
  - `operational`: Admits all terminal tasks regardless of model vintage for complete 90-day
    system accounting, diagnostic failure decomposition, and historical tracking.
- `--as-of`: Optional ISO 8601 reference timestamp (defaults to current UTC).
- `--lookback-days`: Evidence observation window in days (default: 90). Tasks with
  terminal timestamps outside `[as_of - lookback_days, as_of]` are reported under
  exclusions as `outside_window`.
- `--min-samples`: Minimum completed/failed task count required per
  `(task_class, profile, mutation_mode)` cell (default: 10). Cells with fewer samples
  are marked ineligible with stable reason `insufficient_sample_size`.
- `--json-output` / `--markdown-output`: Optional file paths to write deterministic
  sanitized outputs. If neither is specified, the Markdown report is printed to stdout.

#### Data handling and interpretation

- **Task-level aggregation**: Each task is counted exactly once regardless of
  retry attempts or number of worker runs, preventing retry loops from inflating sample sizes.
- **Inclusion criteria**: Requires terminal (`completed` or `failed`) Temporal tasks
  running in `native_agent` mode with a valid `TaskSpec`, pinned profile, and
  consistent terminal timeline events. Cancelled, in-progress, malformed, smoke, or
  inconsistent tasks are categorized under exclusions.
- **Stage applicability**: Unconfigured verification, independent review, and
  delivery stages are treated as not applicable (`None`) rather than failures.
- **Manual overrides**: Tasks executed with manual worker overrides are included as
  valid empirical evidence but tracked and reported separately.
- **Candidate ranking**: Eligible candidates are ranked by:
  1. Accepted task rate Wilson 95% confidence interval lower bound (descending).
  2. Median time to terminal in seconds (ascending).
  3. Profile name (ascending) for deterministic tie-breaking.
- **Actionable recommendation fallbacks**: A recommendation is emitted only when
  at least two eligible compatible profiles exist for a given `(task_class, mutation_mode)`.
  If fewer than two eligible candidates exist, `recommended_profile` remains `None`
  and an actionable fallback reason is recorded.
- **Public data boundary**: Generated outputs pass through an explicit allowlist
  validator that forbids task IDs, task text, repository URLs, branch names, summaries,
  logs, artifact URIs, and secrets.

### M29 provider reliability robustness analysis

The M29 provider reliability robustness analysis evaluates the stability and sensitivity of candidate rankings under observation perturbations without changing live routing or introducing automated pass/fail policy thresholds. It analyzes one bounded 90-day PostgreSQL observation snapshot across window variations, temporal half-splits, and deterministic bootstrap resampling.

Generate the robustness report using the operator CLI:

```bash
export DATABASE_URL="postgresql+psycopg://..."

.venv/bin/python scripts/e2e/run_provider_reliability_robustness.py \
  --database-url-env DATABASE_URL \
  --min-samples 10 \
  --bootstrap-iterations 10000 \
  --bootstrap-seed 29 \
  --json-output evaluation/m29_provider_reliability_robustness_report.json \
  --markdown-output evaluation/m29_provider_reliability_robustness_report.md
```

#### CLI options and parameters

- `--database-url-env`: Name of the environment variable storing the database connection URL. In PostgreSQL environments, the transaction is executed with `SET TRANSACTION READ ONLY`.
- `--as-of`: Optional ISO 8601 reference timestamp (defaults to current UTC).
- `--lookback-days`: Observation snapshot lookback window in days (fixed at 90).
- `--min-samples`: Minimum completed/failed task count required per candidate profile cell (default: 10).
- `--bootstrap-iterations`: Number of bootstrap resampling iterations (default: 10,000).
- `--bootstrap-seed`: Deterministic base random seed for bootstrap resampling (default: 29).
- `--json-output` / `--markdown-output`: Optional file paths to write deterministic sanitized outputs. If neither is specified, the Markdown report is printed to stdout.

#### Data handling and interpretation

- **Window variation**: Evaluates candidate recommendations across 30, 60, and 90-day lookback windows (`[as_of-30d, as_of]`, `[as_of-60d, as_of]`, `[as_of-90d, as_of]`) to detect sample-sparsity fallbacks and ranking changes over time.
- **Temporal split-half cohorts**: Partitions tasks into non-overlapping historical `[as_of-90d, as_of-45d)` and recent `[as_of-45d, as_of]` cohorts to test for temporal drift in provider capability with zero overlap at the 45-day boundary. If the historical cohort has zero tasks, the report status is marked `partial`.
- **Deterministic bootstrap resampling**: Runs 10,000 bootstrap iterations with seed 29. Whole task observations are resampled independently within each eligible candidate profile cell, preserving sample sizes and the empirical correlation between acceptance and latency.
- **Stable per-group seeding**: The base seed is combined with `(task_class, mutation_mode)` via SHA-256 digest to ensure deterministic reproducibility per candidate pool regardless of execution order.
- **Ranking parity**: Candidate ranking strictly matches production: Wilson 95% confidence interval lower bound (descending), median latency in seconds (ascending, missing latency treated as infinite), and profile name (ascending).
- **Eligibility gating & descriptive reporting**: Bootstrap is executed only when at least two profiles meet the sample floor; otherwise an explicit `insufficient_data` result with fallback reason is emitted. Winner counts, probabilities, and rank distributions are reported descriptively without automated "safe to route" thresholds.
- **Exclusion accounting**: Root exclusions describe the full 90-day observation snapshot scanned from the database.
- **Public data boundary**: Generated JSON and Markdown artifacts are validated by `assert_sanitized_robustness_report()` to ensure zero leak of task IDs, user prompt text, repositories, branch names, logs, artifacts, or secrets.

### M29 live evidence wave harness

The M29 live evidence wave harness (`scripts/e2e/run_m29_evidence_wave.py`) manages reproducible, resumable execution of frozen evidence suites against real provider runtimes:

```bash
# 0. Preflight smoke check (validates live container model resolution without evaluation contamination)
.venv/bin/python scripts/e2e/run_m29_evidence_wave.py smoke

# 1. Initialize a new evidence bundle (Wave 2: 20 docs cases, 10 pairs)
.venv/bin/python scripts/e2e/run_m29_evidence_wave.py init \
  --bundle-dir artifacts/m29_evidence_bundle_wave2 \
  --suite-path evaluation/m29_live_provider_suite_wave2.json \
  --build-sha "$(git rev-parse HEAD)" \
  --target-repository-revision "$(git rev-parse origin/master)" \
  --ack-live-read-only-evidence

# 2. Check execution status and cell progress
.venv/bin/python scripts/e2e/run_m29_evidence_wave.py status \
  --bundle-dir artifacts/m29_evidence_bundle_wave2 \
  --suite-path evaluation/m29_live_provider_suite_wave2.json

# 3. Execute the suite sequentially (with optional --preflight-smoke)
.venv/bin/python scripts/e2e/run_m29_evidence_wave.py run-batch \
  --bundle-dir artifacts/m29_evidence_bundle_wave2 \
  --suite-path evaluation/m29_live_provider_suite_wave2.json \
  --repo-key code-agent \
  --branch master \
  --timeout-seconds 900 \
  --preflight-smoke

# Wave 3: 40 read-only cases (20 investigation + 20 feature, 10 per provider/cell)
.venv/bin/python scripts/e2e/run_m29_evidence_wave.py init \
  --bundle-dir artifacts/m29_evidence_bundle_wave3 \
  --suite-path evaluation/m29_live_provider_suite_wave3.json \
  --build-sha "$(git rev-parse HEAD)" \
  --target-repository-revision "$(git rev-parse origin/master)" \
  --ack-live-read-only-evidence

.venv/bin/python scripts/e2e/run_m29_evidence_wave.py status \
  --bundle-dir artifacts/m29_evidence_bundle_wave3 \
  --suite-path evaluation/m29_live_provider_suite_wave3.json

.venv/bin/python scripts/e2e/run_m29_evidence_wave.py run-batch \
  --bundle-dir artifacts/m29_evidence_bundle_wave3 \
  --suite-path evaluation/m29_live_provider_suite_wave3.json \
  --repo-key code-agent \
  --branch master \
  --timeout-seconds 900
```

#### Bundles and Diagnostic Baselines
- **Wave 1 Diagnostic Baseline**: `evaluation/m29_live_provider_suite.json` (28 tasks across investigation, feature, and docs), preserved immutably at `artifacts/m29_evidence_bundle_wave1_diagnostic/bundle.json`. Captures the historical `gpt-5.4-mini` retirement event.
- **Wave 2 Live Evidence**: `evaluation/m29_live_provider_suite_wave2.json` (20 docs tasks across 10 balanced pairs), tracked at `artifacts/m29_evidence_bundle_wave2/bundle.json`. Powered to meet the canonical sample floor ($N=10$ vs $10$).
- **Wave 3 Live Evidence**: `evaluation/m29_live_provider_suite_wave3.json` (40 read-only tasks across 20 balanced investigation/feature pairs), tracked privately at `artifacts/m29_evidence_bundle_wave3/bundle.json`. The ignored bundle pins the harness build and target repository revision, and preserves terminal outcomes without reruns.

#### Invariants & failure semantics
- **Strict Read-Only Delivery**: All evidence cases enforce `delivery_mode=summary`, low risk, read-only mode, and zero changed files.
- **Early Smoke Exclusion**: Smoke tasks specify `exclude_from_provider_reliability: True` in task constraints and are rejected early by the extractor as `evaluation_smoke`.
- **Fail-Closed Gate Checks**: Any cancellation, pending interaction, or malformed timeline is flagged immediately as a gate failure.
- **Resumable Execution**: In-flight task IDs are tracked in `bundle.json`. If interrupted, re-running `run-batch` resumes polling the active task without creating duplicates.
- **Terminal Truth**: Once a case reaches a terminal status (`completed` or `failed`), it is recorded permanently in `bundle.json` and is never rerun or replaced.


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

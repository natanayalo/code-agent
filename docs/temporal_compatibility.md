# Temporal compatibility baseline

## Qualified target

The repository compatibility target is Temporal Server **1.24.2** with Python
SDK **1.30.0**. The SDK is exactly pinned in `pyproject.toml`; the lock resolves
the same version. This is a repository-tested baseline only. It does not claim
upstream maintenance or support for this version pair. Worker Deployment
Versioning remains disabled. Baseline checks use the existing unversioned
workers and patch-based workflow evolution.

The real-server suite starts an isolated `temporalio/auto-setup:1.24.2` with a
temporary Postgres 16 data directory, bound only to `127.0.0.1:17233`. The test
refuses the default Temporal port, uses unique workflow IDs and task queues,
and does not read the operator stack's address. Never point this suite at the
operator's running stack.

## Validation

Start and stop the disposable stack locally:

```bash
docker compose -p temporal-compat-local -f docker-compose.temporal-compatibility.yml up -d --wait
TEMPORAL_COMPATIBILITY_ADDRESS=127.0.0.1:17233 .venv/bin/pytest -q tests/integration/test_temporal_compatibility.py
docker compose -p temporal-compat-local -f docker-compose.temporal-compatibility.yml down --volumes --remove-orphans
```

The dedicated step in `.github/workflows/pytest.yml` starts the same isolated
Compose project and fails if the server cannot start or any capability
assertion fails. The generic integration step excludes this server-only file;
the dedicated step always runs it. Run the broader Temporal regression set and
the vertical slice with:

```bash
CODE_AGENT_ENABLE_TASK_SERVICE=0 CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 CODE_AGENT_TEST_POSTGRES_URL=postgresql+psycopg://temporal:temporal@127.0.0.1:15432/code_agent_test TEMPORAL_COMPATIBILITY_ADDRESS=127.0.0.1:17233 .venv/bin/pytest tests/unit/test_temporal_workflow_primitives.py tests/integration/test_temporal_runtime.py tests/integration/test_temporal_command_outbox.py tests/integration/test_temporal_fanout_replay.py tests/integration/test_task_endpoints_replay.py tests/integration/test_temporal_compatibility.py
CODE_AGENT_ENABLE_TASK_SERVICE=0 CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 CODE_AGENT_TEST_POSTGRES_URL=postgresql+psycopg://temporal:temporal@127.0.0.1:15432/code_agent_test .venv/bin/pytest tests/integration/test_vertical_slice_e2e.py
CODE_AGENT_ENABLE_TASK_SERVICE=0 CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 CODE_AGENT_TEST_POSTGRES_URL=postgresql+psycopg://temporal:temporal@127.0.0.1:15432/code_agent_test TEMPORAL_COMPATIBILITY_ADDRESS=127.0.0.1:17233 .venv/bin/pytest --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories --cov=sandbox --cov=tools --cov=workers --cov-branch --cov-fail-under=90
```

The real-server suite exercises `TaskExecutionWorkflow` with deterministic
activity doubles, including Queries, an approval Update, an approval Signal,
worker shutdown/restart during a human wait, cancellation, and terminal result
projection. It also creates a paused test-only read-only schedule, triggers it,
checks its workflow result, and deletes it in cleanup. It creates no production
schedule and changes no task contract. Temporal Server 1.24.2 requires workflow
Updates to be enabled explicitly; the isolated stack mounts a test-only dynamic
configuration file for that capability and does not alter operator configuration.

## Workflow patch inventory

Preserve every marker in `orchestrator/temporal/workflows.py`:

| Marker | Branch protected |
| --- | --- |
| `task-delivery-acceptance-v1` | delivery acceptance result semantics |
| `m25-1b-temporal-node-wave` | decomposed node-wave execution |
| `m25-2-bounded-selective-fanout` | versioned node selection/fan-out contract |
| `m25-4-temporal-completion-loop` | verifier/review repair loop |
| `m25-6-cancelled-terminal-state` | cancellation terminal projection |
| `m28-2-rejected-session-state` | rejected session state persistence |
| `m30-approval-decision-persistence` | approval decision persistence |

The existing patch-false replay cases remain required. A marker can be retired
only through staged deprecation, replay verification against frozen sanitized
histories, and evidence that affected executions and retained resettable
histories no longer require its old branch. Removing a marker from current code
does not rewrite histories that have already recorded it.

The checked-in sanitized snapshots are in
`tests/integration/fixtures/temporal_histories/`. To intentionally regenerate
the approval, repair, and decomposed snapshots, set
`TEMPORAL_HISTORY_FIXTURE_DIR=tests/integration/fixtures/temporal_histories`
when running `tests/integration/test_temporal_fanout_replay.py`. Set the same
variable when running the isolated real-server compatibility suite to capture
its approval-update and cancellation histories. Review generated JSON before
replacing committed evidence.

## Release and incompatible workflow changes

Before deploying an incompatible workflow change:

1. Stop new ingress using the existing operator controls and confirm the API,
   Telegram, and webhook entrypoints no longer accept new tasks.
2. Reconcile pending outbox commands and enumerate all open workflows, including
   approval, clarification, and permission-escalation human waits.
3. Drain open executions on compatible workers, or obtain and record explicit
   operator disposition for each execution that cannot drain. Never terminate
   workflows automatically.
4. Replay the retained frozen history set and run the isolated real-server gate
   on the exact candidate commit and server/SDK pair.
5. Deploy the new worker code, verify worker readiness and terminal
   reconciliation, then restore ingress through the same controls.

Keep finite task workflows unchanged. Future recurring work must use Temporal
Schedules that create separate `TaskExecutionWorkflow` executions. A future
long-running coordinator requires a separately designed Continue-As-New state
contract and deduplication strategy. Do not enable Worker Deployment Versioning
as part of this baseline; adopting it requires a separately qualified server
and runtime change.

## Rollback constraints

Roll back worker code only to a build that can replay the histories already
written by the candidate. Keep the prior worker build available until candidate
executions drain or are explicitly disposed. Do not roll back by deleting
histories, force-terminating workflows, removing patch branches, or changing
Temporal persistence schema. Server upgrades, schema changes, schedule
management APIs, alerting, dashboard work, and active recurring jobs are
outside this baseline. Any failed capability check blocks scheduling; do not
expand the version pair silently.

The CI workflow provides a check but does not enforce branch protection. Making
it a required status check is an external repository-settings follow-up.

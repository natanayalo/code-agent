# M24.9.5d — Legacy Runtime Drain Inventory

## Decision

The M25.3 release evidence gate is accepted. PR 4A made Temporal unconditional
for new task submission and worker startup and removed the Postgres task
polling/lease scheduler plus the LangGraph lifecycle. PR 4B removes the
remaining task lease schema and WorkerNode load accounting after the
[snapshot-backed rollback rehearsal](m25_3_slice_4b_schema_evidence.md).

## Post-4A classification

| Area | Current location | Disposition |
| --- | --- | --- |
| Task submission | `orchestrator/execution_submission_service.py` | Always persist `temporal` and enqueue a durable start command. |
| Temporal lifecycle | `orchestrator/temporal/` | Sole production lifecycle owner. |
| Task queue claims and leases | Historical migrations only | Runtime model and current schema removed. |
| WorkerNode registry | `repositories/sqlalchemy_worker.py` | Keep registration, profiles/capabilities, heartbeat/offline health, failure quarantine, success, and operator policy. |
| WorkerNode task-load accounting | Historical migrations only | `current_load` and its constraints removed; `capacity` remains registration metadata. |
| Orchestration domain callables | `orchestrator/graph.py`, `orchestrator/nodes/` | Keep plain routing, approval, memory, worker, verification, review, delivery, and persistence callables used by Temporal activities. |
| Lifecycle compiler/checkpoints | None | LangGraph compilation, edges, interrupts, and checkpoint support removed. |
| Postgres product projections | `db/`, `repositories/` | Keep task/run/timeline/API/dashboard evidence separate from Temporal history. |

## WorkerNode method audit

Kept:

- registration and process identity
- worker type, profile, and capability metadata
- heartbeat, offline recovery, and stale-health sweep
- provider/infrastructure failure accounting and quarantine
- successful-run health reset
- operator-policy and supported-worker/profile lookup

Removed:

- task-claim load reservation and release
- load reconciliation after task lease expiry/cancellation
- task-to-worker matching used by the retired scheduler

Temporal execution-capacity permits, activity heartbeats, DAG-node fencing, and
outbox command claims are separate mechanisms and remain in service.

## PR 4B completion

PR 4B removes task `lease_owner`, `lease_expires_at`, and `next_attempt_at`
plus WorkerNode `current_load`. Upgrade, downgrade, re-upgrade, snapshot
restore, and legacy-image compatibility passed on disposable PostgreSQL
databases. The live database remains on the pre-4B revision until deployment.

The following task fields remain product policy/evidence and are not migration
candidates: `attempt_count`, `max_attempts`, `priority`, and `queue_lane`.

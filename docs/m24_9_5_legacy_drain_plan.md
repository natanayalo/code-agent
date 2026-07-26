# M24.9.5d — Legacy Runtime Drain Inventory

## Decision

The M25.3 release evidence gate is accepted. PR 4A makes Temporal unconditional
for new task submission and worker startup and removes the Postgres task
polling/lease scheduler plus the LangGraph lifecycle. The retained
legacy-capable image and matching configuration remain the rollback artifact
until PR 4B completes schema cleanup.

## Post-4A classification

| Area | Current location | Disposition |
| --- | --- | --- |
| Task submission | `orchestrator/execution_submission_service.py` | Always persist `temporal` and enqueue a durable start command. |
| Temporal lifecycle | `orchestrator/temporal/` | Sole production lifecycle owner. |
| Task queue claims and leases | SQLAlchemy columns and migrations only | Live methods and writes removed; columns retained for PR 4B compatibility. |
| WorkerNode registry | `repositories/sqlalchemy_worker.py` | Keep registration, profiles/capabilities, heartbeat/offline health, failure quarantine, success, and operator policy. |
| WorkerNode task-load accounting | Schema fields only | Reservation, release, reconciliation, and task matching removed; fields deferred to PR 4B. |
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

## PR 4B follow-up

PR 4B owns the database migration for task `lease_owner`,
`lease_expires_at`, and `next_attempt_at`, plus any now-unused WorkerNode load
fields. Before migration, take a restorable database snapshot and verify
upgrade, downgrade, re-upgrade, image rollback, and database restore ordering
on a disposable database.

The following task fields remain product policy/evidence and are not migration
candidates: `attempt_count`, `max_attempts`, `priority`, and `queue_lane`.

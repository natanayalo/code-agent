# Temporal Cutover Record

## Purpose

This document is the consolidated historical and rollback record for the
M24.9.5 Temporal runtime consolidation and the M25.3 Temporal-only cutover. It
preserves the decisions and evidence still needed to understand or recover the
released system without keeping completed milestone plans in the active docs.

The immutable release record remains the GitHub release
`m25.3-temporal-cutover-20260726T213001Z`. Git history retains the original
milestone plans, evidence templates, rehearsal instructions, and closeout
documents.

## Final architecture decision

Temporal is the sole durable task scheduler and lifecycle engine:

| Owner | Responsibilities |
| --- | --- |
| Temporal | Lifecycle sequencing, activity dispatch, retries and timeouts, signal waits, cancellation, and bounded DAG coordination. |
| Postgres | Tasks, sessions, interactions, timelines, worker evidence, artifacts, memory, delivery metadata, dashboard queries, and the transactional Temporal command outbox. |
| code-agent domain logic | TaskSpec generation, planning and decomposition, routing, worker/provider behavior, sandbox policy, validation, review, delivery, and memory governance. |

New submissions persist `orchestration_runtime=temporal` and a durable start
command in one transaction. The worker-owned dispatcher delivers start,
signal, and cancellation commands idempotently. Temporal history owns durable
execution state; Postgres remains the operator-facing product projection.

The retired runtime consisted of the Postgres task-polling and lease scheduler,
the LangGraph lifecycle compiler/checkpoint path, and runtime-selection
fallbacks. Reusable orchestration domain callables remain under
`orchestrator/graph.py` and `orchestrator/nodes/` because Temporal activities
invoke them directly.

The following product policy and evidence fields were retained after scheduler
retirement: `attempt_count`, `max_attempts`, `priority`, and `queue_lane`.
Task `lease_owner`, `lease_expires_at`, and `next_attempt_at`, plus WorkerNode
`current_load`, were removed.

## Cutover timeline

| Date | Event |
| --- | --- |
| 2026-07-22 | Local M25.3 rehearsal accepted after the operational scenarios, task classes, and automated suites passed. |
| 2026-07-26 | Release candidate `251b9aaabb683e1a273b83c66cde5632d45b1e65` deployed to the sole local Compose release environment. |
| 2026-07-26 21:30:01Z | Persisted `TEMPORAL_ONLY_CUTOVER_AT` boundary established. |
| 2026-07-27 | Immutable release evidence gate accepted and legacy deletion authorized. |
| 2026-07-28 | PR #340 retired the legacy scheduler, LangGraph lifecycle, and runtime selector. |
| 2026-07-28 | PR #341 removed the legacy lease and WorkerNode load schema after rollback rehearsal. |
| 2026-07-28 | PR #342 hardened Temporal interaction handling and graceful worker shutdown. |

Release identity:

| Field | Recorded value |
| --- | --- |
| Release tag | `m25.3-temporal-cutover-20260726T213001Z` |
| Release candidate | `251b9aaabb683e1a273b83c66cde5632d45b1e65` |
| Environment | Sole local Compose release environment |
| Deployment began | `2026-07-26T22:20:50Z` |
| Cutover timestamp | `2026-07-26T21:30:01Z` |
| Runtime | API and worker both Temporal-only |
| Operator | `natanayalo` |

## Accepted operational evidence

The accepted gate covered all of the following scenarios:

1. authenticated full task lifecycle;
2. approval, clarification, task-spec permission, and worker-originated
   permission escalation through Temporal signals;
3. cancellation while provider work was active;
4. worker restart during a long-running activity;
5. Temporal outage with API reads available and new submissions returning 503;
6. sequential DAG execution;
7. bounded two-node read-only fan-out;
8. replay of older Temporal histories;
9. full Python and pre-commit verification;
10. invalid or retired runtime configuration failing visibly;
11. task inspection remaining available during Temporal unavailability;
12. submission recovery without an API restart;
13. Temporal/Postgres terminal-state reconciliation after worker restart; and
14. replay of existing M25.1 and M25.2 workflow histories.

The rehearsal covered simple read-only work, mutable implementation,
sequential DAGs, fan-out DAGs, approval, clarification, permission escalation,
cancellation, provider retry or restart, and terminal failure.

Key observed results:

- authenticated tasks completed through API, outbox, workflow, worker,
  verification, delivery, and artifact projection;
- a sequential DAG and concurrent read-only fan-out completed, with root-node
  starts observed milliseconds apart;
- cancellation, worker restart, and terminal reconciliation behaved as
  expected;
- an intentionally failed deterministic verification produced a failed task
  while preserving successful worker evidence; and
- the final drain snapshot contained zero active tasks, zero active legacy or
  unknown-runtime tasks, and zero legacy submissions after cutover.

### Focused worker-restart proof

Task `9a9b49ec-a95a-45ad-8063-ef73b43ae05c` ran a fan-out DAG on the release
images. Its two parallel-safe roots began 8.188 ms apart, the join completed,
and `verify_result` began with a 20-second heartbeat timeout. The worker was
restarted while verification was active. Temporal recorded a heartbeat timeout
and activity attempt 2, then completed the workflow. Postgres recorded the
parent task and worker run as successful, all three nodes as completed, and one
terminal `task_completed` event.

## Automated verification at acceptance

- `.venv/bin/pytest tests/unit -q --cov --cov-fail-under=90`: 1,785 passed,
  90.01% coverage.
- `.venv/bin/pytest tests/integration -q`: 343 passed and one
  environment-dependent Postgres-search skip.
- `.venv/bin/pre-commit run --all-files`: passed.
- `cd dashboard && npm run test:coverage`: 291 passed with 95.12% statement
  coverage.
- `.venv/bin/python .agents/skills/e2e-qa/scripts/run_e2e_qa.py`: passed.
- PR #339 focused verification, pytest CI, pre-commit, frozen evaluation, and
  CodeQL checks passed.

These results describe the accepted cutover release. They are historical
evidence, not a claim about the current branch's test counts or operational
readiness.

## Release and rollback artifacts

Release images use tag `m25.3-temporal-cutover-20260726T213001Z`. The retained
legacy-capable images use tag `m25.3-legacy-lkg-20260727`.

| Service | Release image digest | Legacy-capable rollback digest |
| --- | --- | --- |
| API | `sha256:17f92a3a32dd8cfa5892e78b3c16ee59c8371a0910610acd85100410ffccc1a4` | `sha256:060981998c74a61eb799f9babaa26d28d9253b3d98248ada78edbf2484dbe4b3` |
| Worker | `sha256:1370f061ebf330ebe08b0ef27bbcc077102c471f14ec791a27fdbfbe2f857eb8` | `sha256:8b51244ad5ff6bfca228c129ffaa033fdefc86420837d907fbab062e9cee8a46` |
| Migrate | `sha256:36660d0dd14101caf6dcc0b31c4d4848faebee4846af3495dbf84f19d4bd2905` | `sha256:af052c9c20fb361bc63dd29f44e8a621219ea3fb55316bbc40cfa21884d2e27a` |
| Dashboard | `sha256:3e4524464b75ad74a07a141e97d384e1bec1093930aae1e350def53bf885775b` | `sha256:20da0c1214ae7c6ef3b09daab7d6ba5f5f63f45155483a60c02f896ed7603bd2` |

## Schema snapshot and rehearsal

The pre-cleanup PostgreSQL snapshot was created from Alembic revision
`20260720_0046` with no active tasks. It contained 115 terminal tasks, 24
WorkerNode rows, and the legacy lease columns that revision required.

| Field | Recorded value |
| --- | --- |
| Snapshot path | `artifacts/m25_3_slice_4b/20260727T215413Z/pre_migration.dump` |
| Format | PostgreSQL custom format (`pg_dump -Fc`) |
| Size | 1.2 MB |
| SHA-256 | `6ed54402661dbc21f8fcde8c070f966f3254d45c4ac44f8ac73096f36fbad376` |

The snapshot is intentionally ignored because persisted task data may be
sensitive. Retain it locally with the rollback images.

The rehearsal restored the snapshot into disposable databases, upgraded to
`20260728_0047`, verified the removed columns, indexes, and constraints,
performed a new task write, downgraded to `20260720_0046`, re-upgraded, and
verified compatibility with the retained legacy migrate image. Downgrade
restores schema shape but cannot reconstruct dropped lease values.

## Exact rollback procedure

Do not use Alembic downgrade alone as the data-recovery procedure.

1. Stop API and worker services and block new submissions.
2. Revalidate the retained snapshot checksum shown above.
3. Drop and recreate only the code-agent application database.
4. Restore the custom-format snapshot with `pg_restore`.
5. Deploy the API, worker, migrate, and dashboard images tagged
   `m25.3-legacy-lkg-20260727` with their matching legacy configuration.
6. Verify Alembic revision `20260720_0046`, the expected task/run counts, API
   readiness, worker health, and Temporal/Postgres state before accepting new
   submissions.

Forward deployment of the schema cleanup requires the reverse safety order:
stop API and worker services, validate the snapshot, apply `20260728_0047`,
deploy matching application images, then verify readiness and projections
before reopening submissions.

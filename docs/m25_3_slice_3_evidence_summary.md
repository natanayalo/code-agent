# M25.3 Slice 3 — Evidence Gate Closeout

**Status:** local rehearsal accepted on 2026-07-22; immutable release evidence
gate accepted on 2026-07-27.

This is the durable reviewer and next-slice handoff for the local Compose
rehearsal and release gate. The release-specific ledger is attached to the
immutable `m25.3-temporal-cutover-20260726T213001Z` GitHub release and retained
as [sanitized release evidence](m25_3_slice_3b_release_evidence.md).

## Scope and outcome

Slice 3A exercised the Temporal-only cutover evidence gate locally. The
rehearsal covered the 14 operational scenarios, the required task classes,
automated suites, worker recovery, and workflow-history replay.

Slice 3B treated local Compose as the sole release environment. Commit
`251b9aa` was deployed with the preserved `2026-07-26T21:30:01Z` cutover
timestamp. Task `9a9b49ec-a95a-45ad-8063-ef73b43ae05c` ran a bounded fan-out,
then the worker was restarted while `verify_result` was running. Temporal
recorded a heartbeat timeout, retried the activity as attempt 2, and completed
the workflow; Postgres projected the task and worker run as successful. The
operator accepted this evidence and authorized Slice 4 implementation.

## Implementation fixes included

- `e23ac94` prevents the generic clarification policy from blocking the
  deterministic fan-out QA fixture.
- `d82427c` keeps scratch-namespaced fan-out workers from creating a legacy
  provider home in the repository workspace.
- `6593441` filters native-provider runtime files from changed-file audits.
- `ab2edd4` adds regression coverage for bounded workspace prompt guidance and
  restores the Python coverage gate.
- `891eb28` adds recurring heartbeats to the long-running Temporal verification
  activity and configures a 20-second heartbeat timeout.
- `251b9aa` proves recurring verification heartbeats while verification remains
  active.

## Evidence summary

| Area | Local evidence |
| --- | --- |
| Task lifecycle | Authenticated Temporal-owned dummy task completed through API, workflow, worker, verification, and artifact checks. |
| HITL | Clarification, approval, task-spec permission, and worker-originated permission escalation resume paths were exercised. |
| DAGs | Sequential DAG and concurrent read-only fan-out DAG completed in task `4ee125bb-498b-46b6-a810-c95c1177f775`; its fan-out roots began 12 ms apart and terminal Antigravity completion was observed. |
| Recovery | Cancellation during active provider work, worker restart recovery, and Temporal/Postgres terminal reconciliation were observed. |
| Availability | With Temporal stopped, reads remained available and new submissions returned 503; submission recovered after Temporal returned. |
| Replay | Older workflow history plus M25.1B fixture and existing M25.2 sequential/fan-out histories replayed without failure. |
| Terminal failure | A deterministic verification failure projected a terminal failed task while preserving successful worker evidence. |
| Slice 3B recovery | Fan-out task `9a9b49ec-a95a-45ad-8063-ef73b43ae05c` recovered from a worker restart during independent verification: Temporal attempt 2 followed a heartbeat timeout and both Temporal and Postgres completed. |
| Slice 3B drain | Zero active tasks, zero active legacy tasks, zero active unknown tasks, and zero legacy submissions since cutover. |
| Slice 3B rollback | Legacy-capable API, worker, migrate, and dashboard images retain tag `m25.3-legacy-lkg-20260727`. |

The worker-originated escalation used a temporary local provider-denial harness
to deterministically exercise the persistence, signal, and retry path; the
retry itself used the restored real provider. Treat this as local system-path
evidence, not a live-provider production incident.

## Automated verification

- `.venv/bin/pytest tests/unit -q --cov --cov-fail-under=90`: 1,785 passed,
  90.01% coverage.
- `.venv/bin/pytest tests/integration -q`: 343 passed, 1 environment-dependent
  Postgres-search skip.
- `.venv/bin/pre-commit run --all-files`: passed.
- `(cd dashboard && npm run test:coverage)`: 291 passed; 95.12% statement coverage.
- `.venv/bin/python .agents/skills/e2e-qa/scripts/run_e2e_qa.py`: passed.
- PR #339 focused verification: 16 unit tests and 14 Temporal integration tests
  passed; CI pytest, pre-commit, frozen eval, and CodeQL checks passed.

## Slice 4 handoff

Slice 4 remains separately scoped: delete legacy dispatch/lifecycle code first,
then remove legacy schema fields in a second migration PR. The release gate now
retains:

- a versioned legacy-capable rollback artifact;
- an immutable deployment-specific copy of the evidence ledger with the actual
  `TEMPORAL_ONLY_CUTOVER_AT` value and drain snapshot; and
- the corresponding operator approval.

See [m25_3_temporal_cutover_verification.md](m25_3_temporal_cutover_verification.md)
for the operational procedure and
[m25_3_observation_ledger.md](m25_3_observation_ledger.md) for the release
record template.

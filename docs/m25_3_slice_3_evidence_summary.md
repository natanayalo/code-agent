# M25.3 Slice 3 — Evidence Gate Closeout

**Status:** local rehearsal complete by operator acceptance on 2026-07-22;
production evidence gate pending.

This is the durable reviewer and next-slice handoff for the local Compose
rehearsal. It summarizes what was verified; it is not the immutable,
deployment-specific release ledger described in
[m25_3_observation_ledger.md](m25_3_observation_ledger.md).

## Scope and outcome

Slice 3A exercised the Temporal-only cutover evidence gate locally. The
rehearsal covered the 14 operational scenarios, the required task classes,
automated suites, worker recovery, and workflow-history replay. The operator
accepted the local rehearsal evidence and authorized Slice 4 planning. Slice
3B remains open until deployment-specific immutable release evidence is
recorded and approved.

The detailed, mutable local rehearsal log is intentionally kept outside the
repository. It contains no release authority and must not be substituted for a
production evidence artifact.

## Implementation fixes included

- `e23ac94` prevents the generic clarification policy from blocking the
  deterministic fan-out QA fixture.
- `d82427c` keeps scratch-namespaced fan-out workers from creating a legacy
  provider home in the repository workspace.
- `6593441` filters native-provider runtime files from changed-file audits.
- `ab2edd4` adds regression coverage for bounded workspace prompt guidance and
  restores the Python coverage gate.

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

## Slice 4 handoff

Slice 4 remains separately scoped: delete legacy dispatch/lifecycle code first,
then remove legacy schema fields in a second migration PR. Before any production
legacy deletion, the release deployment must retain:

- a versioned legacy-capable rollback artifact;
- an immutable deployment-specific copy of the evidence ledger with the actual
  `TEMPORAL_ONLY_CUTOVER_AT` value and drain snapshot; and
- the corresponding production operator approval.

See [m25_3_temporal_cutover_verification.md](m25_3_temporal_cutover_verification.md)
for the operational procedure and
[m25_3_observation_ledger.md](m25_3_observation_ledger.md) for the release
record template.

# M25.3 Slice 3B — Immutable Release Evidence

This sanitized ledger records the release gate for the sole local Compose
runtime. The copy attached to GitHub release
`m25.3-temporal-cutover-20260726T213001Z` is the immutable release record.

## Release identity

| Field | Recorded value |
| --- | --- |
| Release candidate | `251b9aaabb683e1a273b83c66cde5632d45b1e65` |
| Environment | Sole local Compose release environment |
| Deployment began | `2026-07-26T22:20:50Z` |
| `TEMPORAL_ONLY_CUTOVER_AT` | `2026-07-26T21:30:01Z` |
| Runtime | API and worker both `temporal` |
| Fan-out | Enabled for the release proof |
| Operator | `natanayalo` |
| Release tag | `m25.3-temporal-cutover-20260726T213001Z` |

The deployed worker copies of `orchestrator/temporal/activities.py` and
`orchestrator/temporal/policy.py` byte-matched the corresponding files in
`251b9aa`.

## Release and rollback images

| Service | Release image digest | Legacy-capable rollback digest |
| --- | --- | --- |
| API | `sha256:17f92a3a32dd8cfa5892e78b3c16ee59c8371a0910610acd85100410ffccc1a4` | `sha256:060981998c74a61eb799f9babaa26d28d9253b3d98248ada78edbf2484dbe4b3` |
| Worker | `sha256:1370f061ebf330ebe08b0ef27bbcc077102c471f14ec791a27fdbfbe2f857eb8` | `sha256:8b51244ad5ff6bfca228c129ffaa033fdefc86420837d907fbab062e9cee8a46` |
| Migrate | `sha256:36660d0dd14101caf6dcc0b31c4d4848faebee4846af3495dbf84f19d4bd2905` | `sha256:af052c9c20fb361bc63dd29f44e8a621219ea3fb55316bbc40cfa21884d2e27a` |
| Dashboard | `sha256:3e4524464b75ad74a07a141e97d384e1bec1093930aae1e350def53bf885775b` | `sha256:20da0c1214ae7c6ef3b09daab7d6ba5f5f63f45155483a60c02f896ed7603bd2` |

Release images use tag `m25.3-temporal-cutover-20260726T213001Z`. Rollback
images retain tag `m25.3-legacy-lkg-20260727`. Schema compatibility remains
unchanged in this release; rollback uses those images with the preserved
pre-Slice-4 schema and configuration.

## Evidence carried forward

The operator explicitly accepted the Slice 3A rehearsal evidence and retained
it for scenarios unaffected by the verification-heartbeat fix. The
[Slice 3 closeout](m25_3_slice_3_evidence_summary.md) records the 14-scenario
matrix, all 10 task classes, full automated suites, availability tests, HITL,
cancellation, terminal failure, and replay coverage.

Against the deployment that established the cutover timestamp:

- mutable lifecycle tasks `4cb5ee71-05dc-4ac7-8f8e-cc63787f1ed5`,
  `d5170c56-57bc-4e58-b5a5-a57b77d3e2c5`, and
  `4e1ee4bd-c076-4aad-9178-04e80f992046` completed through Temporal;
- sequential DAG task `44e6d48d-501d-4593-aceb-aaef6ccf4ca3` completed; and
- fan-out task `45649d70-fcec-4498-ac84-77cde1a36903` exposed the missing
  verification heartbeat and was terminated through the approved cancellation
  endpoint.

PR #339 supplied the fix and regression coverage. Its 16 focused unit tests,
14 Temporal integration tests, pytest CI, pre-commit, frozen eval, and CodeQL
checks passed.

## Focused worker-restart proof

Task `9a9b49ec-a95a-45ad-8063-ef73b43ae05c` ran on the release images with
`orchestration_runtime = temporal`.

- Parallel-safe nodes 1 and 2 began at `22:31:03.482273Z` and
  `22:31:03.474085Z`, an 8.188 ms separation, and both completed.
- The join node completed at `22:34:14.535499Z`.
- Temporal scheduled `verify_result` with `HeartbeatTimeout: 20s` at
  `22:34:14Z`.
- The verifier subprocess was observed running and the worker was restarted at
  `22:34:15Z`.
- Temporal recorded activity attempt 2 at `22:34:36Z` with last failure
  `Heartbeat timeout` and `TimeoutType: Heartbeat`.
- Activity attempt 2 completed at `22:34:45Z`; the Temporal workflow completed
  at the same timestamp.
- Postgres recorded the parent task as `completed`, worker run
  `4423b0d0-ed52-4cc3-90fc-31b5c13202dd` as `success`, all three nodes as
  `completed`, and a terminal `task_completed` timeline event.

The generic E2E helper's final history-order assertion returned a false
negative because the second root's `ActivityTaskStarted` event appeared after
the first completion in fetched Temporal history. Persisted node-attempt start
times provide the fan-out overlap evidence above; the task, recovery, and
terminal-projection assertions all passed.

## Final drain and decision

Snapshot taken after task completion:

| Gate | Result |
| --- | --- |
| Active tasks | `0` |
| Active legacy tasks | `0` |
| Active unknown-runtime tasks | `0` |
| Legacy submissions since `2026-07-26T21:30:01Z` | `0` |
| Temporal / API / worker health | Healthy |
| Rollback images retained | Passed |
| Operator sign-off | Approved |

**Evidence-gate decision:** approved for Slice 4.

**Decision date:** `2026-07-27`.

This approval authorizes the separately scoped Slice 4 code-deletion and
schema-cleanup PRs. It does not itself delete legacy code or schema.

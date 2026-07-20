# M25.3 Slice 3 — Temporal Observation Evidence Ledger

Use this blank release-record template for the production-like deployment that
begins the M25.3 observation window. It is an audit aid, not evidence by
itself. Copy it to a release-specific record in the approved immutable release
artifact before replacing placeholders; never overwrite this blank template or
a prior observation record. Do not record credentials, secrets, or raw
sensitive logs here; link to the approved release artifact instead.

## Deployment identity

| Field | Recorded value |
| --- | --- |
| Deployment image / revision | `<image-or-revision>` |
| Deployment environment | `<production-like-environment>` |
| Deployed at (UTC) | `<ISO-8601 UTC timestamp>` |
| Immutable `TEMPORAL_ONLY_CUTOVER_AT` | `<ISO-8601 UTC timestamp>` |
| Operator | `<name-or-identifier>` |
| Last-known-good rollback image / revision | `<image-or-revision>` |
| Rollback configuration and schema compatibility evidence | `<approved-artifact-reference>` |

## Pre-observation admission gate

Complete this gate before running the prerequisite Compose scenarios. An
ambiguous historical task cannot be allowed to retain unresolved runtime
ownership while the scheduler boundary is being evaluated.

| Check | Required evidence | Status / reference |
| --- | --- | --- |
| Active unknown drain | `/metrics` shows `active_unknown_task_count = 0`, and every previously active unknown task is explicitly classified, completed, or cancelled. | `<pending>` |

**Admission decision:** `<passed / blocked>`
**Decision recorded at (UTC):** `<timestamp>`
**Operator:** `<name-or-identifier>`

## Prerequisite Compose scenarios

Complete every scenario against the deployment above before starting the
seven-day active soak. A passed row must identify the executed task or
workflow when one exists and link the supporting evidence.

| # | Scenario | Executed at (UTC) | Task / workflow ID | Outcome | Evidence reference | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Authenticated Compose task completes its full lifecycle. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 2 | Approval, clarification, and permission escalation resume through Temporal signals. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 3 | Cancellation while a provider activity runs reaches the expected terminal projection. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 4 | Worker restart during an activity recovers through Temporal. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 5 | Temporal outage keeps API reads available and returns 503 for submissions. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 6 | Sequential DAG task completes. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 7 | Two-node read-only fan-out task completes. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 8 | Older Temporal workflow history replays after deployment. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 9 | Full Python suite and pre-commit complete. | `<timestamp>` | `n/a` | `pending` | `<reference>` | |
| 10 | Mismatched API and worker runtime configuration fails visibly. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 11 | With Temporal unavailable, `/tasks` rejects submissions while inspection remains available. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 12 | After Temporal recovers, a new submission succeeds without API restart. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 13 | After worker restart, Temporal and Postgres terminal states reconcile. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 14 | Existing M25.1 and M25.2 workflow histories replay after deployment. | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |

**Scenario gate decision:** `<all 14 passed / blocked>`
**Decision recorded at (UTC):** `<timestamp>`
**Operator:** `<name-or-identifier>`

Start the active soak only when every scenario is passed. A failure, missing
evidence reference, or unresolved divergence blocks the soak; investigate or
roll back with the compatible last-known-good image rather than automatically
falling back to the legacy runtime.

## Deployment changes during observation

Record every deployment change while this release record is active. A material
runtime, workflow, schema, or configuration change restarts the seven-day
active soak unless the operator explicitly records why evidence continuity is
valid.

| Changed at (UTC) | Image / revision | Change summary | Material change? | Continuity decision and operator | Evidence reference |
| --- | --- | --- | --- | --- | --- |
| `<timestamp>` | `<image-or-revision>` | `<summary>` | `<yes-or-no>` | `<restart-soak-or-documented-continuity>` | `<reference>` |

## Stage 1 — Seven-day active soak

| Check | Required evidence | Status / reference |
| --- | --- | --- |
| Soak start and end (at least seven complete days after the scenario gate) | UTC timestamps and release record | `<pending>` |
| No accidental legacy submissions | `/metrics` snapshots show `legacy_submissions_since_cutover = 0` | `<pending>` |
| No severe Temporal incident | Incident record or explicit no-incident attestation | `<pending>` |
| No product-state divergence | Task/workflow and Postgres projection review | `<pending>` |
| All 14 prerequisite scenarios remain passed | Completed table above | `<pending>` |

**Stage 1 decision:** `<passed / blocked>`
**Decision recorded at (UTC):** `<timestamp>`
**Operator:** `<name-or-identifier>`

## Stage 2 — Retirement gate

Do not begin legacy deletion until every row is satisfied. Stage 2 may overlap
with Stage 1, but it cannot pass until both the elapsed-time and task-count
thresholds have been met.

All task-class evidence must identify the task ID, created and completed UTC
timestamps, `orchestration_runtime = temporal`, deployment/revision, and an
evidence reference. The 25-task completion population must be demonstrated by
the task IDs or a reproducible database/query artifact; tasks count only when
they completed after `TEMPORAL_ONLY_CUTOVER_AT` against the observed deployment
or a documented compatible successor.

| Gate | Required evidence | Status / reference |
| --- | --- | --- |
| Observation duration | At least 14 days since the cutover timestamp | `<pending>` |
| Temporal completions | At least 25 successful Temporal-owned tasks completed after `TEMPORAL_ONLY_CUTOVER_AT` against the observed deployment or a documented compatible successor | `<pending>` |
| Completion population evidence | The 25 qualifying task IDs or a reproducible database/query artifact | `<pending>` |
| No legacy submissions | `/metrics` shows `legacy_submissions_since_cutover = 0` | `<pending>` |
| Legacy drain | `/metrics` and task review show zero unfinished legacy tasks | `<pending>` |
| Active unknown drain | `/metrics` shows `active_unknown_task_count = 0`, with every previously active unknown task explicitly classified, completed, or cancelled | `<pending>` |
| Simple read-only task class | Successful task ID and evidence | `<pending>` |
| Mutable implementation task class | Successful task ID and evidence | `<pending>` |
| Sequential DAG task class | Successful task ID and evidence | `<pending>` |
| Fan-out DAG task class | Successful task ID and evidence | `<pending>` |
| Approval task class | Successful task ID and evidence | `<pending>` |
| Clarification task class | Successful task ID and evidence | `<pending>` |
| Permission-escalation task class | Successful task ID and evidence | `<pending>` |
| Cancellation task class | Task ID and terminal-state evidence | `<pending>` |
| Provider-retry task class | Task ID and retry evidence | `<pending>` |
| Terminal-failure task class | Task ID and terminal-state evidence | `<pending>` |
| No stuck workflows | Temporal and task-projection review | `<pending>` |
| No projection mismatches | Reconciliation evidence for workflow and Postgres state | `<pending>` |
| Operator sign-off | Named operator approval and release-record reference | `<pending>` |

## Closeout decision

**Retirement gate decision:** `<approved for Slice 4 / blocked>`
**Decision recorded at (UTC):** `<timestamp>`
**Approving operator:** `<name-or-identifier>`
**Rollback evidence reference:** `<approved-artifact-reference>`

An approved retirement gate authorizes planning the separately scoped Slice 4
deletion PRs; it does not itself delete the legacy runtime, change deployment
configuration, merge code, or deploy a release. The closeout evidence must
show both `active_legacy_task_count = 0` and `active_unknown_task_count = 0`.

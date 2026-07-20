# M25.3 Slice 3 — Temporal Observation Evidence Ledger

Use this blank release-record template for the production-like deployment that
begins the M25.3 observation window. It is an audit aid, not evidence by
itself: replace every placeholder with the corresponding deployment evidence.
Do not record credentials, secrets, or raw sensitive logs here; link to the
approved release artifact instead.

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

| Gate | Required evidence | Status / reference |
| --- | --- | --- |
| Observation duration | At least 14 days since the cutover timestamp | `<pending>` |
| Temporal completions | At least 25 successful Temporal task completions | `<pending>` |
| No legacy submissions | `/metrics` shows `legacy_submissions_since_cutover = 0` | `<pending>` |
| Legacy drain | `/metrics` and task review show zero unfinished legacy tasks | `<pending>` |
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
configuration, merge code, or deploy a release.

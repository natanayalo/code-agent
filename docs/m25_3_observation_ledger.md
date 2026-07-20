# M25.3 Slice 3 — Temporal Evidence Gate Ledger

Use this blank template for the release-specific record that demonstrates the
M25.3 evidence gate. It is an audit aid, not evidence itself. Copy it to an
approved immutable release artifact before replacing placeholders; never
overwrite this template or a prior release record. Do not record credentials,
secrets, or raw sensitive logs here; link to the approved artifact instead.

## Deployment identity

| Field | Recorded value |
| --- | --- |
| Deployment image / revision | `<image-or-revision>` |
| Deployment environment | `<development-environment>` |
| Deployed at (UTC) | `<ISO-8601 UTC timestamp>` |
| `TEMPORAL_ONLY_CUTOVER_AT` | `<ISO-8601 UTC timestamp>` |
| Operator | `<name-or-identifier>` |
| Last-known-good legacy-capable image / revision | `<image-or-revision>` |
| Rollback configuration and schema compatibility evidence | `<approved-artifact-reference>` |

## Operational scenarios

Complete all 14 scenarios. Scenarios 9 through 12 may cite the specified
passing automated-test evidence instead of a manual Compose run. Manual
scenarios must record the timestamp, task or workflow ID when applicable,
outcome, and supporting evidence. Automated-test scenarios must record the
test name, suite-run timestamp, pass/fail result, and CI URL or local-output
reference.

| # | Scenario | Satisfaction method | Execution or suite timestamp (UTC) | Task / workflow ID or test name | Outcome | Evidence reference | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Authenticated Compose task completes its full lifecycle. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 2 | Approval, clarification, and permission escalation resume through Temporal signals. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 3 | Cancellation while a provider activity runs reaches the expected terminal projection. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 4 | Worker restart during an activity recovers through Temporal. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 5 | Temporal outage keeps API reads available and returns 503 for submissions. | Manual Compose | `<timestamp>` | `<id-or-n/a>` | `pending` | `<reference>` | |
| 6 | Sequential DAG task completes. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 7 | Two-node read-only fan-out task completes. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 8 | Older Temporal workflow history replays after deployment. | Manual Compose | `<timestamp>` | `<workflow-id>` | `pending` | `<reference>` | |
| 9 | Full Python suite and pre-commit complete. | Integration test / suite | `<timestamp>` | `<suite-or-test-name>` | `pending` | `<reference>` | |
| 10 | Mismatched API and worker runtime configuration fails visibly. | Integration test | `<timestamp>` | `<test-name>` | `pending` | `<reference>` | |
| 11 | With Temporal unavailable, `/tasks` rejects submissions while inspection remains available. | Integration test | `<timestamp>` | `<test-name>` | `pending` | `<reference>` | |
| 12 | After Temporal recovers, a new submission succeeds without API restart. | Integration test | `<timestamp>` | `<test-name>` | `pending` | `<reference>` | |
| 13 | After worker restart, Temporal and Postgres terminal states reconcile. | Manual Compose | `<timestamp>` | `<id>` | `pending` | `<reference>` | |
| 14 | Existing M25.1 and M25.2 workflow histories replay after deployment. | Manual Compose | `<timestamp>` | `<workflow-id>` | `pending` | `<reference>` | |

### Required automated-test mappings

Use these exact tests when recording automated evidence for scenarios 10
through 12. Do not substitute a test whose assertion does not establish the
listed behavior.

| Scenario | Required automated evidence |
| --- | --- |
| 10 | `tests/unit/test_worker_runtime_entrypoints.py::test_execution_runtime_defaults_to_temporal_with_explicit_legacy_fallback` proves invalid runtime selection raises visibly. |
| 11 | `tests/unit/test_task_routes.py::test_submit_task_returns_503_without_persisting_when_temporal_is_unavailable` proves a failed `POST /tasks` returns 503 while `GET /tasks` remains available. |
| 12 | `tests/unit/test_task_execution_service_persistence.py::test_temporal_submission_succeeds_after_recovery_without_service_restart` proves one service instance first rejects an unavailable submission, then persists a Temporal-owned task and `start` command after recovery. |

## Task-class coverage

One task may cover multiple classes. All manual scenario and task-class
evidence must run against the deployment identity above, show
`orchestration_runtime = temporal`, and record its execution or completion
timestamp. Rerun the relevant evidence after a later code, workflow, schema,
or material runtime-configuration change invalidates it.

| Class | Task ID | Runtime | Completion timestamp (UTC) | Deployment revision | Classes covered by this task | Evidence reference |
| --- | --- | --- | --- | --- | --- | --- |
| Simple read-only | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Mutable implementation | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Sequential DAG | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Fan-out DAG | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Approval wait | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Clarification wait | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Permission escalation | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Cancellation | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Provider retry or restart | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |
| Terminal failure | `<pending>` | `temporal` | `<timestamp>` | `<revision>` | `<classes>` | `<reference>` |

## Automated suites

| Suite | Result | Executed at (UTC) | Evidence reference |
| --- | --- | --- | --- |
| `.venv/bin/pytest tests/unit -q --cov --cov-fail-under=90` | `<pending>` | `<timestamp>` | `<reference>` |
| `.venv/bin/pytest tests/integration -q` | `<pending>` | `<timestamp>` | `<reference>` |
| `.venv/bin/pre-commit run --all-files` | `<pending>` | `<timestamp>` | `<reference>` |
| `cd dashboard && npm run test:coverage` | `<pending>` | `<timestamp>` | `<reference>` |

## Closeout decision

| Gate | Status / reference |
| --- | --- |
| All 14 operational scenarios passed or are satisfied by the permitted integration-test evidence | `<pending>` |
| All 10 task classes covered | `<pending>` |
| All automated suites green | `<pending>` |
| Runtime drain snapshot | `<timestamp>` and evidence show `active_legacy_task_count = 0`, `active_unknown_task_count = 0`, and `legacy_submissions_since_cutover = 0` |
| Last-known-good legacy-capable image tagged | `<pending>` |
| Operator sign-off | `<pending>` |

**Evidence-gate decision:** `<approved for Slice 4 / blocked>`
**Decision recorded at (UTC):** `<timestamp>`
**Approving operator:** `<name-or-identifier>`
**Rollback evidence reference:** `<approved-artifact-reference>`

An approved gate authorizes the separately scoped legacy-deletion PRs. It does
not delete the legacy runtime, change deployment configuration, merge code, or
deploy a release.

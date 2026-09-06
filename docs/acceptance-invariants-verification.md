# Task acceptance verification and trial accounting

Recorded 2026-09-06. This record distinguishes observed checks from earlier
supervisor reports. It does not retroactively change historical task statuses.

## Scope and deployed revision

Branch: `task/enforce-delivery-acceptance-invariants`.
Local API and worker were rebuilt at
`732e370bf7e02efb8dd111ec7eb3be313c37be1e`. No production merge was performed.
Required verification failures, unavailable verification, and missing broker
delivery evidence now prevent completion. Fatal setup failures retain workspace
identity and block execution; missing lockfiles have not become advisory.

## Live service checks

Both final checks used authenticated `POST /webhook` against the existing
allowlisted `qa-dummy` repository, without replacing its contents or changing
credential scope. The API, persisted timeline, and Temporal result were inspected.

| Task | Result | Evidence |
| --- | --- | --- |
| `78ce6eb0-214e-46d7-91fd-e2cfd30fc406` | Expected failure | Read-only worker reported success; required `python3 -c "raise SystemExit(23)"` failed. API and Temporal returned `failed`; verification retained `test_regression`; timeline contained `task_failed` and no completion/delivery event; no changed files. |
| `f5561c7e-379d-416a-8107-33eb572b97a3` | Happy-path check blocked | Codex executor exhausted its usage allowance before editing. API and Temporal returned `failed`; no delivery metadata or completion event. This is not evidence of successful branch delivery. |

An earlier negative smoke, `5afcb85d-40b8-406a-b46c-27d016b1cb9c`, ran at
`f8ce1dcf8d3c42ca91601f31d8cb212af6850831`. It correctly failed acceptance but
exposed the read-only deterministic-failure downgrade. The final negative smoke
above verifies the subsequent correction. These are distinct platform checks,
not additional completed target-repository tasks.

The outstanding pre-merge check is a successful live branch or draft-PR delivery
after executor quota is available. Broker-confirmed success and duplicate activity
delivery are covered by integration tests, but those do not replace this live check.

## Automated checks

Using `.venv/bin/pytest` with the same source packages and branch coverage as
`.github/workflows/pytest.yml`:

- `tests/unit tests/workers`: **2,383 passed**, 3 dependency warnings.
- `tests/integration`: **357 passed**, 1 PostgreSQL test skipped, 44 warnings.
- The skipped PostgreSQL full-text-search test was then run with
  `CODE_AGENT_TEST_POSTGRES_URL` using its isolated database fixture: **1 passed**.
- Combined coverage after those runs: **89.96%**, below the **90%** gate.
  This gate has not passed; the draft is not merge-ready. No coverage exclusions
  or threshold reductions were introduced to hide the gap.
- Isolated `orchestrator.acceptance` coverage: **100% of lines and branches**,
  with 20 acceptance cases passing before the final additional missing-result
  regression. A combined-coverage diff inspection found 87/88 added executable
  lines covered; the one uncovered failure-construction line received that
  additional regression test.

Exact coverage invocation: append
`--cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories
--cov=sandbox --cov=tools --cov=workers --cov-branch --cov-report=term`
to the unit/worker command, then add `--cov-append --cov-fail-under=90`
for integration and the PostgreSQL follow-up. Pytest printed a coverage failure
despite exit code zero in this environment; the numeric report is authoritative.

## Historical trial corrections

GitHub GraphQL independently verified these values on 2026-09-06:

| fpl-horizon PR | State | PR head revision | Master merge revision |
| --- | --- | --- | --- |
| #2 | Merged | `9ae99d10de276a2ce4e04d51332d60a77ee54f3f` | `aafe0b85169e52074ce5ff0efafb32b44e53eb7f` |
| #3 | Merged | `6a599642c4b6679b59743625351ee371d0a7ead8` | `ce8ca0815b4883d189c9c4c957d6825ac3049068` |
| #4 | Merged | `26d007c9667feac68b8f1d834d43b9cc3ccbb7fa` | `e8e1ff8ebf15310496170568ee877028e61eaa29` |
| #5 | Open | `588c1ed34fc9dda424698e6c59b82dad899d8645` | None |

Earlier tables called PR head revisions merge revisions; use the separate columns
above instead. Historical elapsed times remain attributed to the supplied trial
reports unless independently remeasured. Human active time and supervising-agent
active effort were not instrumented and are **unavailable**, not verified minute
counts. Do not aggregate their earlier estimates into productivity claims.

Provisioning, interpreter, classification, and acceptance repairs belong to
platform work. Target task durations may include retries, but the same platform
repair effort must not also be charged as target implementation time. The reported
`f4eb3fbe` classifier result is separate from its incomplete coding delivery;
historical `completed` alone is not evidence of a delivered PR.

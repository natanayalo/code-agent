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

The outstanding pre-merge check is a successful live branch or draft-PR delivery.
A subsequent Terra-supervised attempt, `4b37871e-928f-40e4-ba34-5ccc7e1b221d`,
created the requested file and passed deterministic verification. Independent
verification was blocked: the native runner treated inherited executor edits as
verifier mutations, masking Antigravity's authentication error with
`read_only_violation` and preventing fallback. API and Temporal result both returned
`failed`; timeline contained `delivery_failed` and `task_failed`; no branch was
published. Fixing the verifier audit requires separate approval under AGENTS.md.

Independent review also found an existing-PR shortcut that could bypass pushing
the current workspace. That shortcut has been removed: even when a PR already
exists, broker push must succeed before reconciliation. A persisted-state
integration regression confirms that a failed push cannot complete the task.

## Automated checks

Using `.venv/bin/pytest` with the same source packages and branch coverage as
`.github/workflows/pytest.yml`:

- Final `tests/unit tests/workers`: **2,392 passed**, 3 dependency warnings.
- Final `tests/integration`, including PostgreSQL using its isolated database
  fixture: **359 passed**, 45 warnings.
- Combined coverage: **90.04%**, passing the **90%** gate. Focused workspace
  identity and rejected-push tests close the earlier gap; no coverage exclusions
  or threshold reductions were introduced.
- `.venv/bin/python scripts/e2e/run_frozen_eval.py --runner orchestrator`:
  **25/25 passed, score 84/84**. Its mock runner now explicitly requests summary
  delivery because it has no broker workspace or external delivery channel.
- Isolated `orchestrator.acceptance` coverage: **100% of lines and branches**,
  with 20 acceptance cases passing before the final additional missing-result
  regression. A combined-coverage diff inspection found 87/88 added executable
  lines covered; the one uncovered failure-construction line received that
  additional regression test.

Exact coverage invocation: append
`--cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories
--cov=sandbox --cov=tools --cov=workers --cov-branch --cov-report=term`
to the unit/worker command, then add `--cov-append --cov-fail-under=90`
for integration with `CODE_AGENT_TEST_POSTGRES_URL` set. The final combined run
used `COVERAGE_FILE=/tmp/terra-ci.coverage`. Earlier local and GitHub pytest runs
printed coverage failures despite successful process/step status; the numeric
report is authoritative. The final numeric report explicitly passes the gate.

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

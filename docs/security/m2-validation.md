# M2 validation

This report records the historical PR #430 candidate, not validation of the
M32-0 reconciliation with later `master`. Re-run the gates on that candidate;
the results below do not establish current readiness or authorize activation.

## M32-0 reconciliation in progress

The candidate combines `master` at `03d0e8a735912e0aca8365709fe9f6b4eab590ca`
with PR #430 head `c06179d25efb73e6e7d29e7bf542757e68dae2b7` and conflict
resolutions. On 1 November 2026, the broader integration run below passed
598 tests and skipped 31, including the opt-in Docker security and PostgreSQL
checks. The repaired M29 test also passed. These are synthetic results; the
live-provider, credential-refresh, external-access, rollback and activation
gates remain open.

An isolated checkout at candidate commit `1e7db5404e410273d04c6ec0107f55f6dee152b1`
also passed the real pinned ingress proxy, disposable PostgreSQL backup/restore,
and read-only allowlisted local-source mount tests (3 passed). These used the
current non-root user and cached images; they did not exercise the root-equivalent
credential staging case or the direct-egress cases. The checkout's synthetic
receipt is separate from the earlier local receipt.

```sh
CODE_AGENT_M2_DOCKER_TESTS=1 CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  /mnt/c/Users/Natan/Dev/code-agent/.venv/bin/python -m pytest -q \
  tests/integration/test_m2_docker_security.py::test_real_proxy_rejects_noncanonical_requests_before_upstream \
  tests/integration/test_m2_postgres_rollback.py
CODE_AGENT_M2_DOCKER_TESTS=1 CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  /mnt/c/Users/Natan/Dev/code-agent/.venv/bin/python -m pytest -q \
  tests/integration/test_m2_docker_security.py::test_allowlisted_local_source_is_immutable
```

```sh
CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 .venv/bin/pytest -n 4 -q tests/integration \
  -k 'not test_live_adapters_e2e_stack_with_watchdog'
.venv/bin/pytest -q \
  tests/integration/test_m29_mutation_live_e2e.py::test_live_adapters_e2e_stack_with_watchdog
```

The first unit/worker run had 3,301 passes and one failure because Windows
checkout converted three hash-bound reports to CRLF. Their committed LF bytes
matched the manifest; LF checkout attributes were added. The combined run below
then passed **3,902 tests**, with **31 skips** and **91.02% branch-aware overall
coverage**. For changed executable lines in `apps/api/`, `sandbox/`, and
`workers/`, the diff against the candidate's master base intersected with the
coverage report has **260/260 lines covered**. This line count does not by
itself prove every branch was exercised.

```sh
CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 COVERAGE_FILE=/tmp/code-agent-m32-0-coverage \
  .venv/bin/pytest -n 4 -q tests/unit tests/workers tests/integration \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch --cov-fail-under=90
```

The changed-line calculation uses `git -c core.autocrlf=true -c
core.filemode=false diff --unified=0 master` and intersects added line ranges
with executable and covered lines from `coverage json`; Windows checkout
settings are necessary to avoid counting line-ending and mode changes as code.

Audit: 24 October 2026; final verification: 25 October 2026, Asia/Jerusalem. Audited base:
`036bde95c74d517f80c17e6bf32e92d300e3b873`. Evidence below concerns the candidate
source and owned synthetic resources, not an activated Code Agent deployment.
Private local evidence lives in Git-ignored `artifacts/`; no credentials or
FamilyFin records belong in this report.

## Regression matrix

| ID | Synthetic/local status | Live activation status | Evidence and remaining boundary |
| --- | --- | --- | --- |
| M2-01 Public ingress | PASS | UNVERIFIED | Real pinned Nginx on edge + private upstream networks; rejected API/login/session/admin/docs routes, methods, encoded/query variants and oversized input never reached recording upstream. No public ngrok/client test |
| M2-02 Webhook secret | PASS | UNVERIFIED | Disabled/missing/invalid/duplicate/non-ASCII header cases; streamed body bounds; denied requests have no persisted or outbound effects |
| M2-03 Sender authorization | PASS | UNVERIFIED | Both numeric IDs required; group/channel/senderless/bot/forwarded updates and malformed identities cannot submit |
| M2-04 Local dashboard | PASS (API contracts) | UNVERIFIED | Existing login/session/CSRF, task, approval/replay and authenticated webhook regression tests. Desktop UI not manually started |
| M2-05 Docker ports | PASS (configuration/lab) | UNVERIFIED | Every host publication is loopback; real proxy lab port loopback. Candidate service listeners and independent LAN/IPv6 checks await activation |
| M2-06 Untrusted sandbox | PASS (bounded lab) | UNVERIFIED | Native/legacy non-root, read-only rootfs, no capabilities/socket, protected sibling file/volume unavailable, private targets/direct egress blocked, owned resources cleaned. No kernel escape claim |
| M2-07 Trusted worker | PASS (assessment) | UNVERIFIED | Shared socket authority explicitly unresolved; no compromised-worker daemon demonstration on this engine. Independent-engine/VM acceptance pending |
| M2-08 FamilyFin | PASS (preservation) | UNVERIFIED (host isolation) | Original container IDs/images/configuration/state/networks and Serve unchanged. No financial storage mounted/read; shared-daemon compromise remains outside the demonstrated boundary |
| M2-09 Secrets | PASS (synthetic) | UNVERIFIED | Task-only fake auth readable/immutable, original source preserved, path aliases rejected, environment inheritance filtered and cleanup tested. Real token scopes/provider refresh/image-content scan pending |
| M2-10 Execution | PASS (regression/lab) | UNVERIFIED (real providers) | Unit/integration contracts cover provider modes; real synthetic native and persistent/one-shot legacy children work. No authenticated cloud-provider task was launched |
| M2-11 Telegram | PASS (synthetic workflow) | UNVERIFIED | API → time-skipping Temporal → recording worker, progress/final notifications, durable retry/restart and concurrent delivery + restored-DB deduplication. No live messages or registration |
| M2-12 Rollback | PASS (synthetic PostgreSQL state) | UNVERIFIED (live rollback) | Actual Alembic head, custom-format pg_dump/pg_restore into fresh DB; all application table row counts match and task/session/delivery/command identity survives. No real-state restore or old public topology reactivated |

NOT APPLICABLE: a dashboard mobile/Tailscale rollout, FamilyFin real-data approval,
secret rotation and production workload migration were not part of repository
activation. The declined 25 October natural-backup observation is not a gate.

## Executed checks

| Check | Outcome / local evidence |
| --- | --- |
| Full unit/worker suite | **3,152 passed**, 4 dependency deprecation warnings; `m2-unit-release.log` |
| Final focused security unit suite | **243 passed**; failure/cleanup branches included; `m2-security-focused-final.log` |
| Ingress/configuration/registration regression | **75 passed**; `m2-ingress-tests-verified.log` |
| Real container security + PostgreSQL restore | **6 passed**; `m2-runtime-tests-verified.log`, `m2-runtime-results-final.xml` |
| Concurrent duplicate + PostgreSQL restore | **1 passed**; `m2-concurrent-rollback.log`, matching XML |
| API/Temporal/shell read-only workflow | **1 passed**; `m2-shell-smoke-final.log` (real workflow/worker class, recording execution adapters) |
| Launch-error redaction and execution controls | **51 passed**; `m2-control-redaction.log`, including namespaced lifecycle regression |
| Combined branch-aware coverage | **90.64%**, above repository 90% gate; `m2-coverage-summary.txt`, `m2-coverage-final.xml` |
| Changed executable security lines | **260/260 = 100%**, above 95% target; per-file line counts and SHA-256 identities in `m2-changed-coverage-final.json` |
| Source typing | mypy: no issues in the changed source modules |
| Formatting/lint/size | Ruff checks and existing size policy, with existing cohesive-file/function waivers only; no new waiver |
| Compose | Both tunnel and observability profiles pass `config --quiet` with synthetic environment values; no services applied |
| Preservation | Repeated comparison passes for **12 original containers and Tailscale Serve**; `m2-preservation-canonical.json` |

Normal pre-commit hooks passed, including secret scanning, Compose schema,
shellcheck, Ruff, formatting, mypy and size checks; `m2-pre-commit-final.log`.
Initial runs applied normal formatting fixes and were rerun without bypasses.
The first unit run exposed CRLF execution in
the shell script and stale credential-fallback expectations; those were fixed
and the complete unit suite rerun successfully. Initial ingress fixture/import
and proxy lab setup errors were corrected and their affected tests rerun.

### Broad integration limitation — not a green full suite

The first broad run ended with 452 PASS, 14 SKIP, four failures and 22 fixture
errors. Three failures were old wildcard-port assertions; the 22 errors came
from the new test's imported-fixture alias. These were corrected and the affected
ingress/configuration tests rerun successfully.

One unrelated failure remains:
`tests/integration/test_m29_mutation_live_e2e.py::test_live_adapters_e2e_stack_with_watchdog`.
All 20 mutation cases passed their quality checks, but execution exceeded the
test's five-minute future cutoff. Publication correctly rejected a terminal
timestamp after that cutoff. The identical test on an untouched archive of base
commit `036bde95...` also failed with the same cutoff condition in **538.12s**.
Evidence: `m2-baseline-mutation-test.log`. No M29/evaluation source was changed.

Do not describe the broad integration suite as passing. The Code Agent maintainer
owns a separate deterministic-clock/long-run test repair before claiming a clean
full suite; this report grants no merge waiver or automatic release approval.

## Reproduction

Run from the repository's Linux checkout after `poetry install` has established
`.venv`. Use the same explicit virtual environment; do not use global pytest.

```sh
CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  COVERAGE_FILE=artifacts/.coverage-unit-release .venv/bin/pytest -n 4 tests/unit tests/workers \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch

# Supply only an owned disposable PostgreSQL URL; never a production URL.
CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  COVERAGE_FILE=artifacts/.coverage-integration-final .venv/bin/pytest tests/integration \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch

CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  COVERAGE_FILE=artifacts/.coverage-telegram-verified .venv/bin/pytest \
  tests/integration/test_telegram_endpoints.py tests/integration/test_local_infra_config.py \
  tests/unit/test_m2_deployment_contracts.py \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch

CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED=0 \
  COVERAGE_FILE=artifacts/.coverage-shell-smoke-final .venv/bin/pytest \
  tests/integration/test_m2_shell_boundary.py \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch

COVERAGE_FILE=artifacts/.coverage-control-final .venv/bin/pytest \
  tests/unit/test_m2_control_command_redaction.py \
  tests/unit/test_docker_sandbox_container.py tests/unit/test_m2_execution_security.py \
  --cov=apps --cov=db --cov=memory --cov=orchestrator --cov=repositories \
  --cov=sandbox --cov=tools --cov=workers --cov-branch

# Root test harness reproduces broker file ownership; tasks still execute in
# unprivileged containers. No Docker socket is mounted into any test container.
CODE_AGENT_M2_DOCKER_TESTS=1 .venv/bin/pytest -v \
  tests/integration/test_m2_docker_security.py tests/integration/test_m2_postgres_rollback.py

# Final receipt script retains the exact diff/coverage and per-file hash logic.
.venv/bin/python artifacts/m2_finalize_coverage.py
.venv/bin/coverage report --data-file=artifacts/.coverage-release --fail-under=90

# Check the same candidate files, including after the focused commits exist.
mapfile -d '' m2_files < <(git -c core.autocrlf=true diff --name-only -z \
  036bde95c74d517f80c17e6bf32e92d300e3b873)
PATH="$PWD/.venv/bin:$PATH" GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0=core.autocrlf GIT_CONFIG_VALUE_0=true \
  GIT_CONFIG_KEY_1=core.filemode GIT_CONFIG_VALUE_1=false \
  .venv/bin/pre-commit run --files "${m2_files[@]}" \
  --hook-stage pre-commit --show-diff-on-failure
```

The retained coverage receipt script combines branch-aware runs, removes stale
line mappings for changed production modules before using final-run coverage,
and separately refreshes `sandbox/container.py` after a formatting-only line
shift. Its changed-line calculation intersects executable statements with the
diff from the audited base. A naive merge of stale source layouts is not used.
For the Windows checkout's WSL hook run, Git's existing `core.autocrlf=true` and
`core.filemode=false` settings were supplied through `GIT_CONFIG_*`; hooks ran on
all 47 candidate files. The command above reproduces that configuration.

## Sanitized evidence index

| Receipt | Scope |
| --- | --- |
| `m2-preservation-canonical.json`, `m2-preservation-final.log` | Container ID/image/configuration hashes, networks/publications and Serve comparison; no environment values or financial records |
| `m2-runtime-identities.json` | Pinned Nginx index, synthetic image identity and source-base identity; test-only resources |
| `m2-unit-release.log`, `m2-ingress-tests-verified.log`, `m2-control-redaction.log`, `m2-shell-smoke-final.log` | Final regression commands/results within their mocked or workflow boundaries |
| `m2-runtime-results-final.xml`, `m2-concurrent-rollback-results.xml` | Real owned-container and disposable PostgreSQL results |
| `m2-coverage-final.xml`, `m2-changed-coverage-final.json`, `m2_finalize_coverage.py` | Branch coverage, changed-line counts, tested-file hashes and exact calculation |
| `m2-integration-tests-final.log`, `m2-baseline-mutation-test.log` | Original broad-run failures and independent untouched-base reproduction |
| `m2-pre-commit-final.log` | Passing normal hooks after corrections; no bypass |

These local receipts remain Git-ignored. The committed reports summarize them;
they must not be represented as external-client or live-provider evidence.

The actual isolated integration database used a random localhost port, synthetic
credentials and tmpfs data. It was not the Code Agent or FamilyFin database.
The runtime fixtures use owned random resource names, a synthetic protected
volume and exact-name cleanup. Daemon-authority attack simulations require a
different approved independent engine and were not run here.

The pinned Nginx index and synthetic image configuration identity are retained
in `m2-runtime-identities.json`. Provider-specific fake/recording adapters and
synthetic auth files are not evidence that expired OAuth refresh works. No
independent external client, real bot, production restart or sensitive data was
used during verification.

# AGY provider authentication handoff

## Plan and boundary

AGY was given a symlink to the broker's `/root/.gemini` credential path, which the
unprivileged task container cannot access. Its provider bootstrap was empty.
Register only `antigravity-cli/antigravity-oauth-token` as provider authentication,
stage a private file through the existing provider stager, and remove the legacy
symlink/copy path. Keep broker mounts unchanged. AGY alone additionally permits its observed
`daily-cloudcode-pa.googleapis.com` eligibility endpoint and the
`www.googleapis.com/oauth2/v2/userinfo` account lookup and
`lh3.googleusercontent.com` profile-image lookup (host-scoped). AGY treats these
lookups as mandatory during eligibility checking. Gemini retains its
existing allowlist.

The configured `CODE_AGENT_ANTIGRAVITY_AUTH_DIR` takes precedence when accessible;
inside Docker, discovery can use the existing `/root/.gemini` broker mount.
Generic Gemini OAuth credentials are not required or staged for AGY.

The task copy is mode 0600, owned by the task container user, and removed by
existing executor cleanup. AGY can refresh the task copy without changing the
host credential. Access and refresh token fields are registered for redaction.
Failure to remove a legacy token link blocks execution before staging.

## Operator recovery

If the provider rejects the refresh token, run `agy` in your host terminal,
complete browser sign-in, then exit the CLI. Do not paste credentials into tasks
or chat. Retry the service task after login; the broker reads the credential
for each invocation. This assumes the configured auth directory is the host's
`.gemini` directory. A different enrollment directory must be populated through
the existing `scripts/bootstrap_antigravity_auth.sh` workflow.

## Verification

- Unit regressions cover AGY-only bootstrap, missing/unreadable credentials,
  configured path precedence, nested-token redaction, and safe legacy cleanup.
- Integration tests exercise bootstrap, capability grant, secret resolution and
  staging with fake credentials. A network-disabled Docker test proves UID 65532
  can read and refresh its private copy, with source preservation and cleanup.
- Live service verification is recorded below after execution.

This change is based on the delivery acceptance work in PR #383 and should be
reviewed as a dependent change. Reverting it restores the previous AGY handoff;
Codex execution is unchanged.

## Execution evidence (2026-09-07)

Final deployed code: `7f77129ab9b81b775bc723903027e1fccb874484`.

- `95e00e39-f5fc-490f-b789-47b93ffeac3c`: token handoff worked; eligibility
  endpoint was blocked. The task required approval because its text mentioned auth;
  the existing operator authorization was applied through the task approval API.
- `cafd7b90-f50a-4c4a-8fa2-e93900449c7f`: eligibility advanced to blocked user-info lookup.
- `66dd5a86-30fc-404c-9563-cc1220ec73f2`: user-info advanced to blocked profile-image lookup.
- `50f90f9b-21a7-4b88-8200-616c00260553`: **completed**, worker `antigravity`,
  profile `antigravity-native-executor-read-only`, native exit 0, 26.35 seconds,
  zero changed files, no provider fallback. README description was correct.
  Deterministic verification and independent verification passed. Overall verification
  was advisory `warning` solely because the read-only summary task reported no tests.

The final main-worker and verifier isolation manifests both identify
`docker_native_agent_executor`, `workspace_mode=read_only`, `github_grant=false`,
`provider_auth_scope=task_scoped_staged_home`, and `cleanup=removed`.
The main task home was confirmed absent after completion. AGY's own
`sandbox_enabled=false` metadata describes its inner CLI setting, not absence
of the outer Docker sandbox.

No interactive login or host credential changes were needed. The enrolled login
successfully authenticated once handoff and provider-specific egress were corrected.

Verification commands and outcomes:

- `.venv/bin/pytest tests/unit tests/workers -q --tb=short`: 2,427 passed after
  initial handoff implementation.
- Full unit/worker coverage run after eligibility endpoint correction: 2,434 passed.
- `.venv/bin/pytest tests/unit/test_antigravity_provider_auth.py tests/unit/test_antigravity_cli_worker_native.py tests/unit/test_provider_bootstrap.py -q --tb=short`:
  39 passed after the final profile-image host addition.
- `.venv/bin/pytest tests/integration -q --tb=short`: 363 passed.
- Full unit/worker plus integration coverage with the repository's eight source
  packages, `--cov-branch`, `--cov-append`, and `--cov-fail-under=90`:
  **90.19%**, threshold explicitly reached. Integration coverage run: 363 passed.
- Changed executable-line coverage against `07ccd806`: 69/71 (**97.18%**).
- All required commit hooks passed, including lint, formatting, mypy, size checks,
  and secret detection.

The full coverage run preceded the final profile-image host addition; that constant
addition is covered by the subsequent focused tests and successful live smoke.

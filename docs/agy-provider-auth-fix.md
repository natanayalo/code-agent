# AGY provider authentication handoff

## Plan and boundary

AGY was given a symlink to the broker's `/root/.gemini` credential path, which the
unprivileged task container cannot access. Its provider bootstrap was empty.
Register only `antigravity-cli/antigravity-oauth-token` as provider authentication,
stage a private file through the existing provider stager, and remove the legacy
symlink/copy path. Keep broker mounts unchanged. AGY alone additionally permits its observed
`daily-cloudcode-pa.googleapis.com` eligibility endpoint; Gemini retains its
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

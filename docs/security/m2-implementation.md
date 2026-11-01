# M2 implementation

## Changes and compatibility

Base: `036bde95c74d517f80c17e6bf32e92d300e3b873`; branch:
`codex/m2-security-hardening`. No database schema change, model-routing change,
deployment permission change, credential rotation or Docker socket removal.

| Area | Result |
| --- | --- |
| API Telegram boundary | Disabled by default. Enabled configuration requires a 32–256 character URL-safe secret and positive numeric sender/private-chat ID lists. Duplicate/malformed headers fail closed before body parsing. Both approved identities, direct private message and non-bot/non-forwarded sender are required |
| Input limits | Telegram-only ASGI middleware buffers at most 256 KiB, including chunked input; noncanonical URI/query forms are rejected. Existing 10,000-character task-text limit remains |
| Delivery | Successful response fields and durable Telegram update-ID deduplication unchanged; authenticated unsupported/unauthorized updates return 200 without persistence or replies |
| Public routing | Digest-pinned non-root Nginx accepts only canonical POST webhook requests. Ngrok is on its edge network, separate from the API network; ingress proxy bridges the two without secrets or a host port |
| Host ports | All publications bind IPv4 loopback. PostgreSQL/Temporal remain available for local repository tooling. Desktop dashboard remains localhost-only; no Tailscale route added |
| Registration | Explicit enablement/config validation before network activity; message-only updates and pending-delivery preservation. Token-bearing request URL/data are absent from argv and raw failure responses are suppressed |
| Credential sources | Worker mounts only Codex `auth.json` and Antigravity token files, read-only. Explicit missing source directories do not fall back to another account; startup no longer defaults to broad home-directory sources |
| Staging | Reject traversal, symlink/hardlink and invalid mount destinations. Provider authentication files get read-only overlays within otherwise writable task homes/caches. Existing native file-secret mounts remain read-only |
| Legacy execution | Non-root identity even under a root broker or Windows host; read-only rootfs, capability removal, no-new-privileges, PID/resource bounds, private IPC and bounded temporary storage |
| Legacy mount policy | Allowlisted local source remotes are always read-only. Shell requests forward read-only policy and the task's scratch namespace into container setup; returned handles preserve the actual namespaced identity for reconnect and cleanup |
| Legacy networking | Networked tasks use the existing native CONNECT proxy on a task-private internal network. Raw default-bridge task networking is removed. Unscoped environment/credential combinations fail closed; secret-bearing network tasks must use native grants with audience restrictions |
| Cleanup and diagnostics | Owned task networks/proxies removed on completion and failures; launch errors omit credential-bearing argv and chained exceptions; bounded Compose logs; ngrok local request inspection disabled; broader build-context secret exclusions |

Python runtime dependencies are unchanged. The ingress proxy is the single added
infrastructure component, directly required to reject unrelated routes before
API access. Its official multi-platform image index was resolved on 24 October:
`nginx:stable-alpine@sha256:47928bf67eb62a1863cb08d9ecb52d3e704304ba614eaee17884204677702951`.
This is reproducible identity, not a vulnerability-scan or release waiver.

The explicitly supported public CA file `cert.pem` remains an image build input;
private keys and local `.env*`/authentication variants are excluded. Do not put
private keys or credential-bearing configuration into that public certificate.

## Intentional migration requirements

- Existing Telegram deployments must explicitly enable ingress and supply both
  allowlists; leaving it disabled cannot authorize tasks even with the API secret.
- Point `CODE_AGENT_CODEX_AUTH_DIR` and `CODE_AGENT_ANTIGRAVITY_AUTH_DIR` at explicit
  Linux/Docker-readable source directories. Compose binds only the required files,
  refuses missing bind sources and does not mount entire auth homes. Optional
  personal CLI configuration is no longer implicitly imported into the worker.
- Provider source tokens remain readable to the trusted worker and selected CLI.
  Real Codex/Gemini/Antigravity authentication and refresh compatibility are an
  activation gate, not established by synthetic token fixtures. A provider that
  needs to rewrite its mounted auth file requires a reviewed refresh design;
  do not remove read-only overlays to make a trial pass.
- Networked legacy/shell use requires an existing Code Agent worker image with
  the native proxy module and Python available. Cleartext HTTP, arbitrary TCP,
  private services and unscoped legacy sandbox credentials are not permitted
  egress. Use broker tools/native grants for explicit credential audiences.
- Local dashboard/API, direct authenticated HTTP webhook, approvals, replay and
  existing native task contracts retain their authentication/state interfaces.

## Deferred architecture

Long polling would eliminate public ingress, but requires approved webhook
deletion, durable offset acknowledgement after persistence, duplicate protection,
single-consumer ownership, reconnect/backoff and operational monitoring. It is
not implemented because restricted webhooks are the smaller compatible change.
See [Telegram's mutually exclusive update modes](https://core.telegram.org/bots/api#getting-updates).

The trusted worker's shared Docker authority remains unresolved. Its dedicated
execution VM recommendation and cutover/rollback contract are in
[m2-operations.md](m2-operations.md). No ordinary network or child-container
control is presented as a solution to trusted-worker compromise.

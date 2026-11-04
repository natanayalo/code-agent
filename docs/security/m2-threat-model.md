# M2 threat model

Audit date: 24 October 2026, Asia/Jerusalem. Implementation base:
`036bde95c74d517f80c17e6bf32e92d300e3b873` (clean `master` before creating
`codex/m2-security-hardening`). Planning used older commit `2e7e059`; it is not
the implementation baseline. This supplements `docs/architecture/threat_model.md`.

## Assets and boundaries

| Boundary | Assets / trusted component | Adversary / consequence |
| --- | --- | --- |
| Internet → ingress | ngrok → restricted Nginx → Telegram API route | Arbitrary HTTP clients; route abuse, memory exhaustion, task injection |
| Telegram transport → operator | Webhook secret, immutable numeric user/chat lists | Unapproved Telegram account or forwarded message; coding authority misuse |
| Desktop → control plane | API secret, dashboard cookie + CSRF/origin checks | Unauthenticated LAN/tailnet client; task/control/diagnostic access |
| Trusted worker → child | Capability grant, selected credentials, task workspace | Hostile repository/model/tool process; sibling data, credentials, internal services |
| Worker → Docker daemon | Unrestricted socket, execution images and host mount authority | Compromised trusted worker; administer every application on that daemon |
| Code Agent → FamilyFin | Separate containers, networks and financial volumes | Shared-daemon administrator or host compromise; financial data loss/exposure |
| Task → model provider | Prompt, code, tool results, provider authentication | Cloud inference disclosure or permitted-host exfiltration |

Telegram's secret header authenticates the transport, not the human. A correct
secret cannot bypass both operator allowlists and the direct-private-message rule.
The ingress proxy rejects unrelated routes independently of application auth.

## Guarantees and limits

Native children and hardened legacy/shell children have non-root identities,
read-only root filesystems, dropped capabilities, no-new-privileges, bounded
resources and only assigned task mounts. Networked children use task-private
internal networks and the existing HTTPS CONNECT proxy, which rejects private
destinations, unsafe DNS answers and SNI mismatches. No Docker socket enters a
task container. This reduces ordinary malicious-task/container risk; it is not a
proof against kernel/runtime escape.

The trusted worker still controls the shared daemon. It can create privileged
containers, mount unrelated named volumes or request host bind mounts available
to Docker Desktop. Separate Compose projects, socket proxies, rootless children
and internal networks do not establish an independent VM boundary. FamilyFin's
volume separation does not withstand compromise of this administrator.

Provider CLIs necessarily read their selected authentication material. Read-only
credential mounts prevent modification, not reading or intentional exfiltration
to a permitted provider. Task-specific copies are not short-lived provider tokens;
their account authority remains that of the source credential. The trusted worker
also holds database, Telegram and repository authority. No security claim relies
on it being unable to inspect its own configuration.

Cloud GPT/Gemini/Antigravity inference sends task information to external providers.
`<private>` redaction is not local-only routing. No new local-routing guarantee or
default model change is introduced. Do not submit material requiring local-only
processing without verifying a supported local route and its actual network use.

## Evidence policy

Confirm configuration facts separately from live exploitability. Do not read
FamilyFin records or mount its volumes for tests. Use synthetic canaries only.
Daemon-authority demonstrations require a separately approved disposable engine;
never grant a test container privileged access to the shared production engine.
Record local, mocked, real-container and independent-network evidence separately.
Real-data FamilyFin approval, host encryption, recovery policy and the declined
25 October scheduled-backup observation remain outside this milestone.

References: [Docker daemon authority](https://docs.docker.com/engine/security/),
[Docker Desktop bind mounts](https://docs.docker.com/engine/storage/bind-mounts/),
[Telegram webhook semantics](https://core.telegram.org/bots/api#setwebhook).

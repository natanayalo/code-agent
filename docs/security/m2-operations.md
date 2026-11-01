# M2 operations and gated activation

## Current state

Repository changes are candidates only. Code Agent is stopped on the audited
engine. No bot webhook, ngrok tunnel, credential, production container, firewall,
Docker daemon or Tailscale route was changed. FamilyFin remains synthetic-only.
Its remaining production decisions are not approved by this work.

All commands below are operational instructions, **not evidence of execution**.
Use a Linux checkout/Poetry virtual environment. On Windows, run the operator
commands from the approved WSL distribution and keep paths Docker-readable.

## Configuration and enrollment

1. Prepare a private deployment environment file, using `.env.example` as a field
   reference. Keep it out of Git and use Windows ACLs as well as Linux permissions
   when it resides on NTFS. Never paste provider/bot/ngrok/API credentials into a
   PR, chat, terminal transcript or Compose configuration output.
2. Generate a webhook secret locally using `secrets.token_urlsafe(32)` and save it
   directly in the protected environment file. It is separate from the bot token
   and dashboard/API shared secret. No rotation of an existing token is implied.
3. Supply `CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS` and
   `CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS` as comma-separated positive numeric IDs.
   Use IDs from a trusted account export or previously operator-reviewed Telegram
   update metadata. For an operator's direct private chat these normally identify
   the same Telegram account; do not use usernames or third-party lookup claims
   as authorization evidence.
4. If IDs are unavailable, perform a separately approved metadata-only enrollment
   capture before enabling task submission. Authenticate the capture with the
   configured webhook secret; retain only `from.id`, `chat.id`, `chat.type` and
   `is_bot`, never invoke the task service, and independently confirm the sender
   from the intended account. Do not call `getUpdates` against a configured
   webhook or delete the webhook without cutover approval. Do not enable any
   allow-all or automatic-enrollment behavior.
5. Set `CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED=1` only after the lists and secret are
   ready. Set `CODE_AGENT_TELEGRAM_DEFAULT_REPO_KEY` to an existing allowed-repo
   key; a URL is not a registry key. Groups, channel posts, anonymous/bot senders
   and forwarded messages are rejected regardless of the lists.
6. Select explicit provider source directories. The worker mounts only the
   configured Codex `auth.json` and Antigravity
   `antigravity-cli/antigravity-oauth-token`. Authenticate outside the running
   worker using the provider's normal protected login flow. Do not use `docker
   compose run worker ... login`: that would grant a temporary service Docker
   authority and its authentication targets are read-only.
7. Check GitHub credential metadata using the configured task credential's own
   account: intended repositories, contents/PR scopes actually needed, expiry and
   revocation. Avoid organization/admin scopes and never substitute the desktop
   user's unrelated token. Metadata validation was unavailable without deployment
   credentials. Maintain the existing reference/scoped-grant mechanism.

## Reviewable activation package

Before any live action, record exact candidate commit, built image IDs/digests,
private configuration snapshot location, rollback images, service/project names,
database volume identity and backup checksum. The sensitive snapshots must stay
in operator-controlled protected storage. Use `docker compose config --quiet`,
never raw `config`, to validate the protected file without printing credentials.

Capture and compare the preservation receipt from PowerShell:

```powershell
./scripts/m2_preservation.ps1 -Receipt ./artifacts/m2-preservation-canonical.json
./scripts/m2_preservation.ps1 -Receipt ./artifacts/m2-preservation-canonical.json -Compare
```

The first command refuses to overwrite an existing receipt. A recorded hash
proves unchanged inspected configuration, not the contents of financial volumes.
M2 never mounts or reads those volumes.

Proposed affected services: API, trusted worker, ngrok, new Telegram ingress and
one-shot registration job; localhost-binding/log changes also affect recreated
Code Agent PostgreSQL/Temporal/UI/dashboard/optional Phoenix services. A stopped
checkout does not prove no persisted Code Agent state exists. Resolve the exact
existing Compose project and database before running migrations or startup.

**Preflight gates:**

- Verify no active task, worker, pending approval or durable command will be
  interrupted. Drain execution before replacing a worker. Record queued tasks and
  approval IDs; do not delete or replay them as a deployment shortcut.
- Back up the Code Agent database with its configured account using a protected
  password file/environment, not a password-bearing command argument. For a
  running existing DB, write `pg_dump --format=custom` directly to protected
  storage. For a stopped DB, obtain approval to start only that database for its
  backup. Test restore into a fresh disposable database and compare task/session/
  inbound-delivery/command records before touching the original volume.
- Preserve the current Compose file, private environment and images. This slice
  has no schema migration; never use `down -v` for activation or rollback.
- Port 8080 currently belongs to unrelated Adminer. Add a reviewed Code Agent-only
  override mapping Temporal UI to `127.0.0.1:18080:8080` using Compose's `!override`
  list replacement on a supported Compose version. Do not stop Adminer or
  accidentally add a second conflicting publication.

  Exact proposed `/protected/code-agent-host-ports.yml` content:

  ```yaml
  services:
    temporal-ui:
      ports: !override
        - "127.0.0.1:18080:8080"
  ```

  Validate this merge with the installed Compose plugin before approval. The
  equivalent synthetic override was validated without starting services.
- Verify provider login and token refresh with each intended provider/runtime in
  the disposable deployment. Test writable caches and read-only auth separately.
  A failed refresh blocks activation until a concrete reviewed solution exists.
- Confirm FamilyFin/M0 IDs, images, states, configuration hashes, networks and
  Serve are unchanged. No new dashboard Tailscale exposure is authorized.
- Present exact `up`/registration commands, service order and rollback route for
  approval. Starting the trusted worker on the shared daemon is explicitly gated.

After approval, use the reviewed project, environment and override explicitly:

```sh
docker compose --project-name code-agent --env-file /protected/code-agent.env \
  -f docker-compose.yml -f /protected/code-agent-host-ports.yml config --quiet
# Only after approval of images, state backup, ports and Docker authority:
docker compose --project-name code-agent --env-file /protected/code-agent.env \
  -f docker-compose.yml -f /protected/code-agent-host-ports.yml \
  up -d postgres temporal temporal-ui migrate api worker dashboard
docker compose --project-name code-agent --env-file /protected/code-agent.env \
  -f docker-compose.yml -f /protected/code-agent-host-ports.yml --profile tunnel \
  up -d telegram-ingress ngrok
# Explicit approved registration action; retain pending deliveries:
docker compose --project-name code-agent --env-file /protected/code-agent.env \
  -f docker-compose.yml -f /protected/code-agent-host-ports.yml --profile tunnel \
  run --rm --no-deps init-webhook
```

`code-agent` is the proposed project name; substitute the verified existing
project if persisted state uses another name. Do not create a second empty
database to bypass uncertain state. Health/readiness and harmless authorized task
checks must pass before declaring activation successful.

Use an independent LAN/tailnet/cellular client for negative raw-port/API/login
probes and a harmless direct message from the approved operator for positive
webhook submission/progress/completion. Confirm unknown identities cannot create
tasks. Own-host HTTP checks are not independent-network evidence. Persisted
deduplication must survive API restart and approved re-registration.

## Rollback

1. Stop only Code Agent's new ngrok/ingress services to remove public exposure.
   Pause new submissions and drain or record active execution; obtain approval
   for any required worker restart.
2. Restore reviewed previous service images, Compose configuration and protected
   environment. Preserve the exact Code Agent database volume. No schema change
   is introduced by M2; verify state before considering a database restoration.
3. Prefer leaving Telegram disabled/public ingress stopped to rolling back to
   optional secret auth or whole-API exposure. Any return to the old public
   topology requires explicit risk acceptance and cutover approval.
4. If state restoration is necessary, restore the validated protected dump into
   a new Code Agent database, verify task/session/delivery/approval continuity,
   and approve the connection switch. Never restore over FamilyFin storage.
5. Recheck local dashboard/API, pending interactions and owned resource cleanup;
   compare the preservation receipt and independently test exposure.

Synthetic rollback evidence is limited to documented tests. Production
PostgreSQL restoration, provider refresh and actual ingress rollback remain gates
until performed against the selected deployment.

## Trusted-worker isolation alternatives

| Option | Boundary / workflow | Cost and Windows implications | FamilyFin consequence |
| --- | --- | --- | --- |
| Shared Docker Desktop | Existing native/legacy workflow; worker retains shared socket and all daemon authority | Lowest operational/resource overhead; no migration | Cannot isolate financial volumes from worker compromise; synthetic coexistence only unless explicitly accepted |
| Dedicated Linux VM | Guest-local trusted control plane, worker and independent Docker engine; guest-owned workspace/credentials | Starting planning budget: 4 vCPU, 8 GiB RAM, 80 GiB expandable guest disk. Linux-filesystem I/O; measure actual provider/build overhead. Home-compatible maintained hypervisor requires feasibility review, or Windows edition upgrade for Hyper-V | Removes ordinary shared-daemon administration of FamilyFin, if no host socket, shared financial folders or privileged host integration is supplied |
| Independent daemon in WSL | Separate daemon data root and explicit endpoint; requires Docker Desktop integration removal, mount/network/service ownership controls for its distribution | Lower incremental overhead but shared WSL kernel and accessible host paths remain; rootless mode has UID/cgroup/network compatibility constraints | Not equivalent to independent VM/host isolation; acceptable only for demonstrated narrower guarantees |
| Separate Linux machine | Existing container execution stack with its own daemon and storage | Additional hardware/maintenance and network latency; repo clones and provider calls remain normal | Stronger separation from this Windows/Docker host; host-level compromise assumptions remain |

Socket endpoint filters cannot safely represent every allowed mount/container
operation as a financial-application isolation policy. Merely moving the worker
to a VM while retaining the Windows daemon socket would not solve A05.

### Recommended follow-up implementation

The implementation target is an Ubuntu 24.04 LTS Generation 2 Hyper-V guest with
its own Docker Engine, four vCPUs, 8 GiB RAM and an expandable 80 GiB VHDX. This
requires a separately approved Windows Home → Pro edition upgrade and Hyper-V
activation. Keep that host maintenance outside M2; back up/rehearse recovery and
verify FamilyFin before and after any approved host change. Do not disable/reset
Docker Desktop, WSL or host security features to install an alternative hypervisor.
[Microsoft Hyper-V edition requirements](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v?pivots=windows&tabs=gui).

If the edition upgrade is declined, use a separate Linux machine for the same
deployment contract. VirtualBox is not the default on this active WSL2 host:
Oracle documents Hyper-V coexistence/performance conflicts and recommends
disabling host virtualization/security features as a workaround, which is
incompatible with preserving the validated deployment without new host approval.
[Oracle troubleshooting](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/Troubleshooting.html).

- Place the Code Agent control plane, database/Temporal and trusted worker in the
  guest. This avoids exposing Windows DB/Temporal listeners to a privileged guest
  worker and preserves existing same-daemon worker/workspace assumptions.
- Keep guest workspaces/database/provider sources on guest-owned Linux storage.
  Clone assigned repositories through narrowly scoped credentials; do not share
  the Windows home, drives, FamilyFin directories, backups, clipboard or Docker
  socket with the guest. Worker mounts only the guest's Docker socket.
- Use NAT networking with only approved loopback forwards needed for the local
  dashboard/API. Keep PostgreSQL/Temporal guest-internal; ngrok still targets the
  guest's restricted proxy. Do not reuse FamilyFin's Tailscale route.
- Enforce host/private-network denials outside the worker-controlled guest for
  destinations other than expressly approved operator connectivity. Guest root
  can change guest firewall rules; it must not administer host-side policy. Local
  inference on Windows requires a separately reviewed narrow network exception.
- Transfer Code Agent state through a protected logical database dump and verify
  sessions/tasks/approvals/deliveries. Stop old execution ownership before guest
  dispatch begins; never run two workers against the same queue/state cutover.
- Verify native/legacy providers, Git operations, approval/cancel/replay, scoped
  egress, cleanup, backup/restore and independent negative canaries. Demonstrate
  that guest worker daemon authority cannot enumerate/mount a synthetic volume
  on Windows's engine; do not test against FamilyFin volumes.
- Rehearse return to old images/state with both ingress and worker drained. Keep
  the original stopped deployment and verified backup until acceptance. Measure
  RAM/CPU/build latency before accepting the provisional resource budget.

This is a follow-up architecture and acceptance contract, not a claim that a
specific hypervisor or cross-engine isolation has been installed or demonstrated.

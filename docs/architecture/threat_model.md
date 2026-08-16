# Threat Model and Execution Trust Boundary Architecture

## 1. Executive Summary & Purpose

This document establishes the authoritative Threat Model and Execution Trust Boundary Architecture for `code-agent` under Phase 4A (Milestone M28.5A).

The primary goal of `code-agent` is to safely execute untrusted and semi-trusted tasks (generated from user prompts, Telegram messages, GitHub webhooks, or issues) using native autonomous coding models (Codex, Antigravity, OpenRouter) inside containerized environments without putting the host system, control plane, credentials, or private codebases at risk.

### Milestone Framing: M28.5A vs. M28.5A.2

- **M28.5A (This Milestone — Foundation & Migration Contracts):** Defines and formalizes the threat boundaries, STRIDE attack analysis, residual risk posture, broker-owned secret registry (`RegisteredSecretDefinition`), broker-issued capability grants (`SandboxCapabilityGrant`), fail-closed resolution engine (`SecretResolver`, `CapabilityGrantFactory`), and migration/deprecation policies for legacy raw credentials.
- **M28.5A.2 (Follow-On Milestone — Runtime Enforcement):** Wires the capability contracts directly into `DockerNativeAgentExecutor` and `WorkerRequest`, proving live OS-level and Docker runtime containment through integration tests (direct socket bypass denial, TLS ClientHello SNI inspection, OS mount masking for `.git`, and `/run/secrets` lifecycle).

---

## 2. Core Trust Boundaries & Architecture

`code-agent` enforces seven distinct trust boundaries across control plane and execution runtimes:

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ Control Plane (Boundary 1)                                              │
│ - API Server / Telegram Ingress / Webhook Receivers                     │
│ - Orchestrator (Temporal Workflow Coordinator)                          │
│ - PostgreSQL Database (Relational Projections & Memory)                 │
│ - Secret Registry & CapabilityGrantFactory                              │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ Broker Task Dispatch
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Sandbox Infrastructure Host (Boundary 2)                                │
│ - Trusted Temporal Worker (Docker Authority)                            │
│ - Host Filesystem & OS Kernel (Root Namespace)                          │
│ - Out-of-Workspace Privileged GIT_DIR (/run/git/repo.git)               │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │ One-Shot Task Container Creation
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Native Agent Execution Runtime (Boundary 3)                             │
│ - Codex CLI / Antigravity CLI / OpenRouter CLI Process                  │
│ - Unprivileged Container (no-new-privileges, cap-drop ALL, read-only fs)│
│ - Task UID/GID Namespace                                                │
├───────────────────────────────────┼─────────────────────────────────────┤
│ Workspace Filesystem (Boundary 4) │ Network & Egress (Boundary 5)       │
│ - Working tree: /workspace (RW/RO)│ - Internal isolated bridge (no GW)  │
│ - Scratch: /workspace/.code-agent │ - Pinned CONNECT Proxy (Port 443)   │
│ - Sibling isolation & no .git mod │ - Full IPv6 ULA & non-global block  │
├───────────────────────────────────┼─────────────────────────────────────┤
│ Secrets & Credentials (Boundary 6)│ Observability & Artifacts (B. 7)    │
│ - Ingress value stripping         │ - Chunk-aware SecretRedactor        │
│ - /run/secrets/code-agent (RO)    │ - Execution manifest scrubbing      │
│ - BROKER_ONLY vs SANDBOX exposure │ - Redacted timeline & trace logs    │
└───────────────────────────────────┴─────────────────────────────────────┘
```

### Boundary Details

1. **Boundary 1: Control Plane**
   - **Components:** FastAPI gateway, Telegram bot ingress, Webhook intake, Temporal workflows, PostgreSQL persistence.
   - **Trust Level:** Highest. Owns authentication keys, database connection strings, and capability issuance.
   - **Protection:** Task-supplied requests are untrusted input. Ingress adapters strip all raw secret values immediately upon intake into `SecretRef(name=...)`. `SandboxCapabilityGrant` instances are derived strictly by server-side orchestrator policy via `CapabilityGrantFactory` and are never accepted or deserialized from user payloads.

2. **Boundary 2: Sandbox Infrastructure Host**
   - **Components:** Host Linux system, Docker daemon socket (`/var/run/docker.sock`), Temporal worker process.
   - **Trust Level:** Trusted infrastructure.
   - **Protection:** Docker daemon authority is confined strictly to the Temporal worker. The Docker socket is never mounted into task containers. Privileged Git operations (commits, branch pushes, PR creations) run broker-side using a detached, broker-owned `GIT_DIR` outside the sandbox mount namespace with `-c core.hooksPath=/dev/null`.

3. **Boundary 3: Native Agent Execution Runtime**
   - **Components:** Codex CLI, Antigravity CLI, or OpenRouter processes executed inside a one-shot container.
   - **Trust Level:** Untrusted / hostile workload. Prompt injection or malicious repository code may compromise the CLI process.
   - **Protection:** Dropped Linux capabilities (`--cap-drop ALL`), `no-new-privileges=true`, read-only root filesystem (`--read-only`), private IPC (`--ipc private`), strict PID limits (256 default), memory bounds (1GiB default, 8GiB max), CPU limits (1.0 default, 4.0 max), and non-root execution.

4. **Boundary 4: Workspace Filesystem**
   - **Components:** Repository working directory (`/workspace`), task scratch space (`/workspace/.code-agent/scratch/`), sibling task directories.
   - **Trust Level:** Task-scoped mutable or read-only tree.
   - **Protection:** Sibling workspaces are strictly forbidden from being mounted or traversed (`is_sibling_workspace` check). In `WORKSPACE_WRITE` mode, direct writes to `.git/config` or `.git/hooks/` are blocked via OS-level mount masking or external `GIT_DIR`. In `SCRATCH_ONLY` mode, writes are restricted to `/workspace/.code-agent/scratch/`.

5. **Boundary 5: Network & Egress**
   - **Components:** Task container network namespace, internal bridge network, CONNECT forward proxy.
   - **Trust Level:** Controlled egress.
   - **Protection:** Direct internet access is structurally impossible (`--network none` by default; when proxy egress is enabled, container joins an internal bridge with no default gateway). All egress routes via the broker CONNECT proxy over port 443 with broker-side DNS pinning, TLS ClientHello SNI inspection, and blocking of all non-global addresses (IPv4 RFC1918, link-local, loopback, CGNAT, and full IPv6 ULA `fc00::/7`).

6. **Boundary 6: Secrets & Credentials**
   - **Components:** OAuth tokens, GitHub tokens, API keys, `SecretRef`, `RegisteredSecretDefinition`, `ResolvedSecret`.
   - **Trust Level:** Least-privilege, ephemeral.
   - **Protection:** Dual-key authorization (`name` in `allowed_secret_refs` AND `scope` in `granted_secret_scopes`). Exposure policy separates `BROKER_ONLY` (tokens kept broker-side) from `SANDBOX_ENV` / `SANDBOX_FILE`. Injected files are mounted read-only under `/run/secrets/code-agent/` (mode `0o400`).

7. **Boundary 7: Artifacts & Observability**
   - **Components:** Captured stdout/stderr, execution manifests, OpenInference tracing spans, Postgres timeline events.
   - **Trust Level:** Public/operator-visible audit record.
   - **Protection:** Chunk-aware `SecretRedactor` scrubs known secret patterns before writing logs, manifests, or timeline events. `ResolvedSecret` uses `__slots__` and redacted string representations to prevent accidental serialization.

---

## 3. STRIDE Threat Analysis & Mitigation Matrix

| Threat Category (STRIDE) | Attack Vector / Scenario | Architectural Mitigation | Enforcement Layer |
|---|---|---|---|
| **Spoofing** | Untrusted task self-asserts capability grant or bypasses worker routing | Grants are issued solely by `CapabilityGrantFactory` from server-side policy; `WorkerRequest` rejects self-asserted grant fields. | Control Plane / Ingress |
| **Tampering** | Sandbox modifies `.git/config` (e.g. `core.sshCommand`) or `.git/hooks` to hijack broker Git operations | Broker uses detached `GIT_DIR` outside sandbox namespace; container `.git` is read-only / masked; broker passes `-c core.hooksPath=/dev/null`. | Sandbox Host / Git Boundary |
| **Tampering** | Task alters injected environment variables to hijack runtime loaders | `destination_env_var` is strictly allowlisted or prefixed `CODE_AGENT_SECRET_`; reserved loader vars (`LD_PRELOAD`, `PYTHONPATH`) rejected. | Secret Registry / Broker |
| **Repudiation** | Malicious agent suppresses command audit or alters timeline records | Execution manifests and timeline events are generated broker-side from container exit code and stdout/stderr capture. | Broker Observability |
| **Information Disclosure** | Untrusted prompt prints injected secret to stdout/logs | `SecretRedactor` scrubs stdout/stderr; `ResolvedSecret` uses `__slots__` and redacted `__repr__`; values never entered in database or tracing. | Observability / Redactor |
| **Information Disclosure** | Untrusted code exfiltrates source code or credentials over public egress | `network=DISABLED` by default; sandbox secrets forbid `PUBLIC_HTTPS_PROXY`; allowed hosts restricted to audience intersection; direct sockets blocked. | Network / CONNECT Proxy |
| **Information Disclosure** | Untrusted code encodes secret into workspace file or commit to exfiltrate via Git push | Sandbox-exposed secrets cannot be combined with `AUTOMATED_EXTERNAL_PUBLICATION` capabilities; publication requires broker-only secrets or human review. | CapabilityGrantFactory |
| **Denial of Service** | Fork bomb, CPU pegging, or memory ballooning crashes worker host | Mandatory finite `ResourceLimits` (CPU <= 4.0, memory <= 8GiB, PIDs <= 1024, timeout <= 3600s) mapped to Docker cgroups. | Docker Container Runtime |
| **Elevation of Privilege** | Container breakout to host via Docker daemon socket | Docker daemon socket is never mounted into container; container runs with `--cap-drop ALL`, `--security-opt no-new-privileges`. | Container Engine / Kernel |

---

## 4. Residual Risks & Virtualization Roadmap

### Residual Risk 1: Shared Linux Kernel Boundary
Standard Linux Docker containers share the host kernel. While capabilities are dropped and seccomp/apparmor profiles are applied, container isolation does not provide a hardware/hypervisor security boundary against zero-day Linux kernel vulnerabilities.

- **Evolution Roadmap:** For untrusted public multi-tenant workloads, `code-agent` plans evaluation of:
  1. **gVisor (`runsc`):** Application-kernel architecture intercepting system calls in user space.
  2. **MicroVMs (Firecracker / Kata Containers):** Hardware-isolated lightweight virtual machines with dedicated guest kernels per task.

### Residual Risk 2: Secret Exfiltration via Permitted Egress or Encoded Output
Any code granted a plaintext secret and permitted network egress can intentionally transmit that secret to the permitted host. Furthermore, malicious code in a writable workspace can encode secrets (via base64, hex, XOR, or file chunking) into workspace files or artifacts.

- **Mitigations in Phase 4A:**
  - High-value action-bearing credentials (`github_token`) are strictly `BROKER_ONLY` by default.
  - Sandbox-exposed secrets forbid broad public egress (`PUBLIC_HTTPS_PROXY`) and require exact host audience intersection.
  - Tasks holding sandbox-exposed secrets cannot be granted automated publication tools (`AUTOMATED_EXTERNAL_PUBLICATION`).

---

## 5. Secret Lifecycle Management

```text
[1. Broker Registration]
    RegisteredSecretDefinition configured with source, scope, exposure policy, and permitted egress hosts.
        ↓
[2. Ingress Intake]
    LegacyIngressTaskRequest receives task; values stripped immediately; only SecretRef(name) preserved.
        ↓
[3. Capability Grant Issuance]
    CapabilityGrantFactory evaluates deterministic policy, checks audience intersection, and issues grant.
        ↓
[4. Runtime Resolution]
    SecretResolver validates dual-key authorization (name in allowed_refs AND scope in granted_scopes).
    Rejects BROKER_ONLY secrets from sandbox resolution.
        ↓
[5. Staging & Execution]
    Secret values registered with task-scoped SecretRedactor.
    Files staged under /run/secrets/code-agent/ (mode 0o400) or env vars prefixed CODE_AGENT_SECRET_.
        ↓
[6. Termination & Cleanup]
    Task container destroyed; temporary secret mounts unmounted and removed; SecretRedactor discarded.
```

---

## 6. Migration and Deprecation Schedule

- **M28.5A (Current):** Typed contracts, threat model, immutable capability grants, ingress DTO separation, and audience intersection.
- **M28.5A.2 (Follow-On):** Runtime wiring into `DockerNativeAgentExecutor`, OS-level `.git` isolation, proxy TLS SNI validation, and live container security verification.
- **M29 (Target Cutoff):** Complete removal of `LegacyIngressTaskRequest` and raw secret ingress. Unregistered or raw secret payloads are rejected fail-closed with `DeprecatedLegacySecretsError`.

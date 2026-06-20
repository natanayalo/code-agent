# Architecture

## Product Model

`code-agent` is a local-first coding agent platform with a strict separation between:

- session/control concerns (platform)
- repo execution concerns (workers + sandbox)
- durable context concerns (memory + persistence)

Core principle: use the platform for cross-run control, and worker runtimes for session-local cognition.

## Layered Architecture

## 1) Platform / Control Plane

Owns request intake, durable state, and run lifecycle governance.

Responsibilities:

- ingress and auth for API/webhook/Telegram
- session + task creation and persistence
- generated TaskSpec contract for task goal/risk/type/delivery policy
- optional LLM orchestrator brain for TaskSpec enrichment and route recommendation
- queueing and lease-based claiming
- orchestration graph execution
- worker routing policy and manual override handling
- approval checkpoints and operator decisions
- replay/retry lifecycle control
- timeline/metrics emission

Primary modules:

- `apps/api/`
- `apps/runtime.py`
- `orchestrator/`
- `repositories/`
- `db/`

## 2) Worker Runtime Layer

Owns provider-specific coding execution behind a shared contract.

Responsibilities:

- adapt generic worker requests into provider-specific runtime calls
- run bounded coding loops with explicit tool boundaries
- emit structured outputs (`status`, `summary`, `commands_run`, `files_changed`, artifacts)
- perform worker-local self-review/fix loops where configured

Active worker/runtime implementations:

- Codex CLI worker (`workers/codex_cli_worker.py`)
- Antigravity CLI worker
- OpenRouter-backed runtime worker (`workers/openrouter_cli_worker.py`)

- `workers/base.py`

### Worker Routing Policy (Current)

Before routing, the orchestrator builds and persists a TaskSpec so workers, APIs, and operator views share an inspectable task contract. When worker profiles are enabled, routing resolves through a capability matrix and pins dispatch to one concrete `WorkerProfile` (`worker_type`, `runtime_mode`, capability tags, permission/mutation policy, and delivery-mode support).

Profile-aware routing toggle and mapping:

- Enable profile-aware routing with `CODE_AGENT_WORKER_PROFILES_ENABLED=1` (wired in API bootstrap).
- Codex/Antigravity default runtime mode is pinned to `native_agent`.
- `CODE_AGENT_CODEX_RUNTIME_MODE` and `CODE_AGENT_CODEX_TOOL_LOOP_LEGACY_ENABLED` are deprecated and ignored; Codex is now native-only and legacy tool-loop profiles are no longer created.
- OpenRouter legacy execution profile is added only when OpenRouter is configured and `CODE_AGENT_OPENROUTER_ENABLED=1`.
- Execution routing then filters profiles by worker availability, execution capability tag, read-only vs patch-allowed mutation policy, and delivery-mode compatibility before selecting a concrete profile.

Current default profile matrix:

- **Codex execution**: `codex-native-executor` with explicit read-only variant `codex-native-executor-read-only`
- **Antigravity execution**: `antigravity-native-executor` with explicit read-only variant `antigravity-native-executor-read-only`
- **Antigravity specialist profiles** (native mode): `antigravity-native-planner`, `antigravity-native-reviewer`, and `antigravity-native-discovery`
- **OpenRouter legacy execution**: `openrouter-tool-loop-legacy` (explicit opt-in only)
- **Optional Codex/Antigravity legacy execution**: `*-tool-loop-executor` profiles are available only
  when the corresponding `*_TOOL_LOOP_LEGACY_ENABLED` env toggle is set.

The selected worker/profile/runtime metadata is persisted on task and worker-run records and returned in task snapshots for operator and dashboard inspection.

## 3) Sandbox + Tool Layer

Owns safe execution of repository mutations and command/tool effects.

Responsibilities:

- provision isolated workspaces and persistent sandbox containers
- execute shell commands through policy gates
- enforce path and permission policies
- redact sensitive data in captured outputs
- capture command/test/diff artifacts and retention metadata

Primary modules:

- `sandbox/`
- `tools/`

### Native Agent Sandbox Policy

For native agent execution, the sandbox boundary depends on the worker profile and environment:

**1. Codex Native Sandbox**

Codex `exec` supports several sandbox modes mapped by repository trust:

1.  **`read-only`**: Used when constraints specify `read_only: true`. No modifications allowed.
2.  **`workspace-write`**: Default for untrusted repos or outside Docker. Uses Codex's internal Linux namespace sandbox.
3.  **`danger-full-access`**: Disables Codex's internal sandbox. Used **ONLY** when running inside a Docker container (`is_in_container()`) **AND** the repository is explicitly trusted via operator-controlled regex patterns (`CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS`).

*Security Guardrails:* Docker is the primary boundary. `danger-full-access` is only allowed inside a container to prevent nested Linux namespace collisions while keeping the process isolated by Docker.

**2. Antigravity Native Sandbox**

The Antigravity CLI uses a boolean sandbox mechanism controlled via `CODE_AGENT_ANTIGRAVITY_NATIVE_SANDBOX_ENABLED`. It defaults to `0` since the primary isolation boundary is the `docker-compose` worker container itself.

## 4) Memory Layer

Owns durable context that survives individual runs.

Responsibilities:

- persist skeptical memory entries with provenance + confidence metadata
- maintain compact session state across turns
- load relevant hints during orchestration
- keep memory inspectable/editable/deletable via API/admin paths

Memory categories in v1:

- personal memory
- project memory
- session/thread state

Primary modules:

- `memory/`
- memory-related repositories in `repositories/`
- schema in `db/models.py` + migrations

## 5) Operator Surfaces

Owns human-facing control and visibility interfaces.

Current operator surfaces:

- local dashboard/PWA for task inspection, timeline visibility, and interaction controls
- task submission/status/replay/approval endpoints (`/tasks`)
- webhook + Telegram ingress routes
- progress notifications (`started`, `running`, terminal)
- health/readiness + operational metrics endpoints

## 6) Future Reflection / Autonomy Layer

Planned, not yet a full implemented subsystem.

Intended responsibilities:

- bounded scout mode for proactive idea generation
- structured friction and improvement proposal pipelines
- operator-curated review queues for suggested changes
- explicit maintenance-action requests (not privileged self-mutation)

This lane remains controlled, inspectable, and human-in-the-loop for high-risk operations.

## Runtime Topology (Today)

```mermaid
flowchart TD
    U[Operator via Telegram or HTTP] --> API[FastAPI Ingress]
    API --> DB[(Postgres<br/>Task Queue)]

    DB --> W[Worker Process]
    W --> ORCH[TaskExecutionService + Orchestrator Graph]
    ORCH --> MEM[Memory + Session State]
    ORCH --> ROUTE[Worker Routing]

    ROUTE --> CW[Codex Worker]
    ROUTE --> GW[Antigravity Worker]
    ROUTE --> OW[OpenRouter Worker]

    CW --> SB[Sandbox Workspace / Container]
    GW --> SB
    OW --> SB

    SB --> TOOLS[Tool Policy + Command Execution]
    ORCH --> ART[Artifacts / Timeline / Metrics]
    ORCH --> DB
```

## Queue + Lease Model

- API writes tasks as pending records.
- Worker process polls queue (`CODE_AGENT_QUEUE_POLL_INTERVAL_SECONDS`, default `2`).
- Worker atomically claims tasks with lease ownership and expiry (`CODE_AGENT_QUEUE_LEASE_SECONDS`, default `60`).
- Heartbeats extend lease while the run is active.
- On success/failure, lease is cleared and status transitions persist.
- Failed attempts are retried up to configured max attempts before terminal failure.

## Safety Boundaries

Hard boundaries currently enforced:

- sandboxed repo execution through dedicated workspace/container flow
- task ingress protected by shared-secret auth
- explicit approval checkpoint flow for tasks requiring manual approval
- callback SSRF protections for outbound progress webhooks
- secret-redaction and command artifact capture for inspection/audit
- budget and tool permission gates in orchestration/worker runtime paths

## Source Of Truth For Behavior

For day-to-day operation and troubleshooting, pair this document with:

- runbook: `docs/runbook.md`
- current operational status: `docs/status.md`

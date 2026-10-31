# M32 — Code Agent Bridge

Status: **Planned; execute the slices below individually.** Planning baseline:
2026-10-31, refreshed `master` at `dbe00739`. This is an implementation plan,
not evidence of deployment readiness or approval to activate services.

## Goal and agreed boundaries

Let the operator use the existing Hermes `local_private` Telegram conversation
to observe Code Agent tasks, then submit scout and coding tasks and cancel
Hermes-created tasks. Code Agent retains execution, persistence, sandbox policy,
human execution approvals, and delivery ownership; Hermes is a scoped client.

- Stage 1 is read-only observation. Stage 2 adds submit/cancel only after
  read-only validation and explicit operator approval of the scope expansion.
- Keep the current Telegram bot and local Ollama model. No separate profile or
  messaging channel is required. Keep `interactive_codex` separate.
- Require a real Hermes confirmation before every submit/cancel call, in
  addition to Code Agent's existing execution approval gates. An approval-system
  failure, timeout, denial, or unattended cron call must cause no mutation.
- Mint `client_id="hermes-assistant"` tokens for an explicit subset of configured
  repository keys. Start synthetic validation with a dummy repository; never
  infer permission for all repositories.
- Hermes may observe tasks from any source within its permitted repositories,
  but may cancel only tasks attributed to its own client identity.
- Never grant Hermes approval decisions, schedule writes, operator credentials,
  provider credentials, Docker access, or host repository mounts. Do not add
  auto-merge, auto-deploy, or autonomous scheduled coding.

## Baseline and prerequisites

[PR #430](https://github.com/natanayalo/code-agent/pull/430) is the Local-hosting
M2 security-hardening foundation. At this planning snapshot it is an open draft,
not merged into this baseline, and reports pending activation/provider evidence
and an incomplete full regression gate. Its reports live on the PR branch;
do not assume `docs/security/m2-*.md` exists on `master` until reconciliation.
Recheck those claims on the candidate revision before implementation or activation.

M2 and M32 are different milestones: Local-hosting M2 hardens Code Agent's
hosting boundary; this repository's M32 adds the scoped assistant bridge.
The trusted worker still has shared Docker-daemon authority. Child sandbox
hardening does not prove isolation from FamilyFin; strong isolation requires a
separately approved VM/independent-engine or separate-host decision.

Current `master` already includes M31 schedule APIs and dashboard management.
The bridge below deliberately exposes task operations only. It does not need
all of M31 to close, does not implement schedule access, and must reject
`schedules:read` until a separate bridge contract is approved.

Source context: the sibling Local-hosting documents `Hermes-Experiment-Plan.md`
(Phase 2) and `Inital-plan.md` (M2), plus the inspected Hermes experiment configs.
They are external planning context, not files this repository can assume are
installed. Preserve their Phase 1 isolation defaults until bridge activation.

## Ordered execution slices

Each slice is a focused PR with its own verification and acceptance evidence.
Keep the stable IDs M32-1 (tokens) and M32-4 (MCP facade). Update
[status](status.md#current-backlog) when starting a slice and link its issue/PR;
do not treat this checklist as merged history.

| Order | Slice | Depends on | Completion gate |
| --- | --- | --- | --- |
| 0 | M32-0: M2 readiness reconciliation | PR #430 and refreshed master | Reviewed hardening candidate, candidate-revision regression evidence, and explicit remaining activation gates. |
| 1 | M32-1: Scoped client tokens | M32-0 repository gate | Revocable, expiring, repository-scoped client authentication with operator-only lifecycle commands. |
| 2 | M32-2: Safe task observation | M32-1 | Sanitized task contracts and server-enforced repository filtering. |
| 3 | M32-3: Attributed task commands | M32-2 | Idempotent scout/coding submission and own-client cancellation through existing services. |
| 4 | M32-4: MCP facade | M32-3 | Authenticated four-tool MCP interface tested against pinned Hermes. |
| 5 | M32-5: Hermes read-only integration | M32-4 and approved M2 activation | Existing local chat can query tasks through a read-only token and private connection. |
| 6 | M32-6: Confirmed write activation | M32-5 acceptance and explicit scope-expansion approval | Confirmed scout/coding submission and cancellation pass the operator-flow checks. |

### M32-0 — M2 readiness reconciliation

- Reconcile #430 with current `master`, address review findings, and rerun the
  documented regression blocker. Do not assume an old failing test still fails
  or that later M29 work resolved it without a candidate-revision result.
- Revalidate restricted public ingress, Telegram secret/identity enforcement,
  loopback-only host publications, sandbox restrictions, and credential staging.
- Verify real Codex and Gemini/Antigravity authentication, including refresh
  behavior with immutable staged auth, plus desktop and Telegram workflows.
- Separate the repository gate from live evidence. Synthetic M32 development
  may proceed after the reviewed repository gate; outstanding real-provider,
  external-access, backup/rollback, and activation evidence blocks M32-5.
- Preserve unrelated containers, FamilyFin data/backup configuration, and
  Tailscale settings. Keep VM/daemon migration and ingress cutover separately
  approved; this slice must not silently perform them.

### M32-1 — Scoped client tokens

- Add operator CLI commands to mint, inspect, revoke, and rotate opaque bearer
  tokens. Store only digests plus token identity, stable client identity,
  expiry, revocation state, scopes, and explicit repository-key restrictions.
  Reveal a token only at minting; default expiry is 30 days.
- Validate persisted token state on every request so revocation takes effect
  immediately. Rotation keeps the stable client identity for attribution and
  deduplication; revoking an old token does not delete its audit history.
- Support exactly `tasks:read`, `tasks:submit`, and `tasks:cancel`. Reject unknown
  scopes, empty repository grants, and keys absent from the configured registry.
  `approvals:decide` and `schedules:write` are permanently forbidden for Hermes.
- Confine bearer authorization to the bridge. Keep current operator header/cookie
  behavior; a supplied bearer token must never fall back to operator credentials
  to widen its permissions. Existing approval/replay/interaction/schedule/admin
  routes must not accept a bridge token as operator authentication.
- Use additive Alembic migrations and repository persistence boundaries. Log
  client identity, operation, authorization outcome, task ID when available,
  and duration without token values, task bodies, or secrets.

### M32-2 — Safe task observation

- Create a versioned, allowlisted projection of existing task snapshots rather
  than serializing the full internal `TaskSnapshot`. Include task ID, repository
  key/branch, status, timestamps, worker, approval requirement, sanitized result
  summary, and structured failure information.
- Exclude raw logs, diffs, credentials, arbitrary interaction payloads, trace
  destinations, and internal artifact paths. Redact private text and credentials
  in bounded summary/error text; treat returned prose as untrusted content.
- Filter authorized repositories before pagination and counts. Unknown or
  out-of-grant tasks return the same not-found response to avoid enumeration;
  tasks without an authorized repository are not exposed.
- Support status, repository and timestamp filters with bounded pagination.
  Use timezone-aware intervals; interpret "today" in the operator's
  `Asia/Jerusalem` timezone and convert the day's boundaries to UTC.

### M32-3 — Attributed task commands

- Accept only repository key, optional branch, task text, scout/coding mode,
  conversation key, and idempotency key. Resolve repositories server-side;
  reject arbitrary URLs, caller identities, callbacks, secret references,
  worker overrides, and unrestricted constraints/budgets.
- Reuse `TaskExecutionService`, scout normalization, existing server budget and
  routing defaults, Temporal submission, and existing execution approval policy.
  Keep policy/orchestration out of protocol handlers.
- Persist nullable `created_by_client_id` on tasks, derived from authenticated
  identity. Existing tasks retain null attribution. Namespace conversation
  identity by client so callers cannot join existing operator sessions by
  supplying a conversation key.
- Reuse the existing delivery-deduplication boundary. Namespace idempotency by
  stable client identity and key; persist a normalized payload digest. Identical
  retries return the original task, changed payloads conflict. Task creation,
  attribution, delivery record, and Temporal outbox command must be atomic and
  remain correct under concurrent retries, restarts, and token rotation.
- Cancel through the existing cancellation service after checking both current
  repository grant and client attribution. Preserve queued/running cancellation
  and repeated/terminal-call semantics. A denied call has no task/outbox effects.

### M32-4 — MCP facade

Mount authenticated `/mcp` in the existing API using the official MCP Python SDK,
locked through Poetry. Use stateless Streamable HTTP with JSON responses and
qualify protocol compatibility with the actual pinned Hermes `v0.22.5` client.
The SDK is the bounded protocol dependency; do not hand-build an MCP transport
or expose the internal tool registry wholesale.

| Tool | Required scope | Input/output |
| --- | --- | --- |
| `list_tasks` | `tasks:read` | Authorized repository/status/time filters and bounded pagination; safe task summaries. |
| `get_task` | `tasks:read` | Task ID; safe task status/result projection. |
| `submit_task` | `tasks:submit` | M32-3 submission contract; task ID and safe snapshot. |
| `cancel_task` | `tasks:cancel` | Task ID; safe cancellation snapshot for an authorized Hermes-created task. |

- Advertise only tools permitted by the current token and reauthorize every
  invocation, including after discovery. Discovery is not an authorization grant.
- Mark reads `readOnlyHint: true` and submit/cancel write-capable. Disable
  resource, prompt, sampling, and unrelated tool capabilities.
- Enforce transport Host/Origin checks for approved private/local endpoints.
  Missing, expired, or revoked credentials produce `401 invalid_token`;
  authenticated forbidden HTTP access produces `403 insufficient_scope`.
  Preserve semantic error codes in protocol-compatible tool errors instead of
  assuming every MCP tool failure is a non-200 HTTP response.
- Return validation/conflict/unavailable errors without raw exceptions. A
  malformed call, unsupported operation, denied scope, or unavailable Temporal
  service must not create partial state.

### M32-5 — Hermes read-only integration

- Add opt-in Compose overrides in Code Agent and Local-hosting to attach only
  the API and Hermes to a dedicated private Docker network. Address the API by
  its bridge service alias; do not rely on `host.docker.internal:8000` reaching
  a host-loopback publication. Publish no new host ports or public MCP route.
- Narrowly extend Hermes' existing private-network guard to that API destination
  and port. Fail closed on missing/invalid destination resolution; retain the
  blocks on other private, LAN, tailnet and host-gateway service targets.
- Store the read-only token in the protected `local_private` environment file
  and reference it through `${CODE_AGENT_HERMES_SCOPED_TOKEN}` in the MCP
  Authorization header. Never pass `CODE_AGENT_API_SHARED_SECRET`, mount host
  auth directories, or add cloud-model credentials.
- Configure `tools.include: [get_task, list_tasks]`, `trust: untrusted`, and
  disabled sampling/prompts/resources using the pinned Hermes configuration
  syntax. The existing illustrative `allowed_tools` snippets need correction.
  Verify discovery and tool execution with the existing profile/platform toolset
  restrictions; a YAML entry alone does not prove usable tools.
- From the current Telegram chat, verify authorized task observation, "What
  tasks failed today and why?", denied repository access, forbidden approval
  calls, and immediate token revocation. Record image/configuration identities
  and preservation evidence for unrelated workloads.
- Update Local-hosting's two source plans and operator verification instructions
  in its own workspace/change. This Code Agent documentation PR does not edit
  sibling-project files or activate their services.

### M32-6 — Confirmed write activation

- After M32-5 acceptance and operator approval, replace the token with one adding
  submit/cancel scopes and expand `tools.include` to the four bridge tools.
  Keep repository restrictions unchanged unless explicitly approved separately.
- Verify the pinned Hermes `trust: untrusted` write gate prompts before every
  mutation. Denial, timeout, missing approver, and unattended execution must
  cause no bridge mutation. Do not substitute a prompt-only instruction or a
  model-supplied `confirmed` boolean for the actual approval surface.
- Run an approved read-only scout, then a bounded coding task on the dummy
  repository. Verify confirmation, attribution, duplicate retries, queued/running
  cancellation, status polling, and normal task lifecycle/delivery.
- Exercise an approval-gated coding task: Hermes cannot decide or answer the
  approval; the task resumes only through Code Agent's existing operator
  surfaces. Prompt injection in returned task text cannot bypass either gate.
- Return task ID/status immediately and support explicit follow-up queries.
  Do not add background polling, a second scheduler, or new progress-delivery
  behavior in this milestone.

## Verification and release evidence

For each implementation PR, apply the relevant [contributor gates](../AGENTS.md):

- Unit tests for token lifecycle, scope/repository checks, projection redaction,
  attribution, payload validation, and every new critical authorization branch.
- Integration tests for migrations (upgrade plus writes and supported downgrade),
  old null-attribution records, transactional outbox behavior, concurrent identical
  retries, conflicting retries, restart persistence, and cross-client denial.
- Real MCP client/server handshake, discovery, malformed calls, scope changes,
  token expiry/rotation/revocation, and task/error contracts with pinned Hermes.
- Focused API/MCP -> Temporal -> sandbox smoke coverage for scout, coding,
  approval/resume, queued/running cancellation, and immutable terminal evidence.
- Hermes approval acceptance/denial/timeout/unattended checks and adversarial
  task-result content. Preserve existing desktop, Telegram, generic webhook,
  GitHub ingress and M31 scheduling/operator behavior.
- Private-network positive/negative checks, unchanged public ingress, no new host
  publications, and before/after preservation receipts for unrelated workloads.

Use the repository virtualenv, normal pre-commit hooks, narrow tests followed by
required broader suites, 90% overall coverage and >=95% changed critical-path
coverage. Record exact commands, outcomes, candidate/image/configuration
identities, evidence limitations and remaining external gates in each PR.
Never infer current readiness from #430's historical test counts.

## Activation, rollback, and completion

The maintainer owns the implementation PRs and evidence; the operator owns
repository enrollment, credentials, security/deployment approvals, live
activation and the Stage 2 scope expansion. This plan grants none of those
permissions by itself.

Rollback starts by revoking the token, disabling the Hermes MCP entry and
detaching the optional bridge network. Retain task and audit records and keep
M2 ingress hardening in place. Do not drop additive attribution/token metadata
until audit/rollback compatibility has been reviewed. Existing Code Agent tasks
continue through their normal lifecycle or explicit operator cancellation.

M32 is complete when both stages have accepted evidence, forbidden operations
and repositories fail closed, every mutation is confirmed, tasks preserve the
existing execution/approval lifecycle, and activation/rollback instructions are
verified. M32 completion does not approve FamilyFin production data or claim
independent host isolation.

## Protocol references

- [Hermes MCP docs at v0.22.5](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/website/docs/user-guide/features/mcp.md)
- [Hermes MCP write-call gate at v0.22.5](https://github.com/NousResearch/hermes-agent/blob/v0.22.5/tools/mcp_tool_handlers.py)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

These are implementation references, not proof that the installed runtime has
passed compatibility or approval tests. Pin the tested versions in evidence.

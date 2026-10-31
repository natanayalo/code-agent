# Roadmap

## Planning principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage from Codex, Antigravity, and OpenRouter over
  rebuilding provider-local cognition
- keep Temporal authoritative for durable coordination and scheduling, the
  sandbox authoritative for effects, deterministic policy authoritative for
  capabilities, and Postgres focused on product projections/durable knowledge
- treat provider execution as evidence rather than task acceptance; keep
  acceptance, verification, review, and delivery outcomes distinct
- default to one strong native coding agent plus verification, independent
  review, and bounded repair; add decomposition/fan-out only for measured needs
- use provider reasoning for coding decisions and deterministic platform code
  for mechanical inspection, validation, verification, evidence collection,
  reconciliation, and capability enforcement
- keep trust-boundary and high-risk changes human-controlled
- require measured real-worker evidence before increasing autonomy

## Planning sources

[Status](status.md#current-backlog) owns current priority order and issue links.
This roadmap owns milestone scope, entry/exit criteria, and conditional future
work. [Historical evidence](archive/phase4a_evidence.md) preserves evaluated
revisions and wave results; [CHANGELOG.md](../CHANGELOG.md) records merged work.

## Current phase

Phase 4A: Temporal stabilization and measured reliability. M25.6, M26, M28,
M28.5, M29, and M30 are complete. M31 is active.

Committed/current priority:

1. M31: Proactive Operations and Safe Scheduled Work

Deferred / evidence-gated:

- M27: Reliability-Based Autonomy Policy

Future / conditional:

- Execution Architecture V2 / durable multi-agent work
- the conditional items documented below; none has a committed milestone
  number

Milestone numbers are stable identifiers; the committed/current sequence above
is the authoritative execution order. M28.5 is the only numbering addition.
M27 stays deferred: it requires broader repeated evidence from M29 and later
operational work and cannot resume from the 20-task baseline alone.

## Product north star

Reduce babysitting for personal coding tasks while keeping execution safe,
inspectable, and reversible.

Primary success metrics:

- human interventions per completed task
- repeated questions per task
- tasks requiring manual log inspection
- validation-evidence rate
- worker and provider failure rate
- success rate by worker profile
- latency by orchestrator stage
- time from submission to terminal state or draft PR
- CI and review rejection rate for generated PRs

The project remains personal-use first. It does not currently optimize for
multi-user SaaS, auto-merge, auto-deploy, broad multi-agent swarms, or
autonomous privileged maintenance.

`code-agent` intentionally accepts more internal operational complexity than
Hermes/OpenClaw-style assistant stacks in exchange for stronger durable
coordination and safety boundaries. Every service and orchestration primitive
must still justify that complexity. The long-term operator experience should
feel like one local appliance even if Postgres, Temporal, API, worker, sandbox,
dashboard, and optional observability/webhook services remain separate.

## Completed foundation

The coding lifecycle, Temporal cutover and recovery, verification/repair,
skeptical memory, bounded DAG execution, execution-boundary contracts,
provider reliability evaluation, and GitHub-native control are complete.

Completed milestone specifications are in the
[historical scope record](archive/completed_milestones.md). Qualification results
are in the [evidence archive](archive/phase4a_evidence.md); merged changes are in
[CHANGELOG.md](../CHANGELOG.md). They are reference material, not backlog items.

## M31 — Proactive Operations and Safe Scheduled Work

### Goal

Move from dashboard-only detection to actionable notification and repeatable
read-only maintenance without introducing unattended mutation risk.

The implemented schedule API uses this lifecycle; natural-language request
interpretation is a planned operator experience, tracked in the M31 backlog:

```text
"Every Monday check outdated dependencies"
        ↓
ScheduleSpec
        ↓
Temporal Schedule
        ↓
normal TaskExecutionWorkflow / TaskSpec pipeline
```

Manual, GitHub, API, Telegram, and scheduled tasks share the same execution,
evidence, acceptance, and delivery lifecycle. Temporal remains the only
scheduler.

### Entry Prerequisite: Supported Temporal Compatibility Baseline

Before introducing long-lived scheduled/proactive workflows, establish and
test a supported compatibility baseline covering:

- the selected Temporal Server version and Python SDK range/version
- Workflow replay compatibility policy and replay tests
- Worker Deployment Versioning strategy
- old-workflow drainage
- Continue-As-New/history strategy where needed
- lifecycle and removal strategy for existing workflow patch markers

This was a bounded architecture/operations prerequisite, not an instruction to
upgrade blindly to the latest release. The selected pair and workflow-evolution
policy have passed the repository compatibility gate. The repository-tested
baseline is Temporal Server 1.24.2 / Python SDK 1.30.0; Worker Deployment
Versioning remains disabled. The compatibility gate and workflow-evolution
policy are documented in
[`docs/temporal_compatibility.md`](temporal_compatibility.md). This does not
claim upstream maintenance support. The weekly dependency-review schedule and
its management API foundation and the opt-in readiness-reason Telegram alert
backend are merged. Read-only dashboard visibility for persisted alert state,
delivery outcomes, schedule details, and submitted task history is also merged
(PR #442); it does not expose schedule mutation controls. Task-specific
stuck-interaction and stale-outbox alerts are also merged (PRs #444 and #446).
Task-specific terminal-reconciliation alerts are merged as well (PR #448).
Provider-capability alerts and opt-in generic webhook delivery are also merged
(PRs #459 and #461). The ordered queue and acceptance conditions are in
[status](status.md#current-backlog).

### Scope

- notify through Telegram when an existing global execution-readiness reason
  becomes degraded or recovers, and for task-specific stuck interactions,
  stale outbox commands, terminal reconciliation divergence, and provider capability
  degradation; optionally deliver sanitized transitions through a generic webhook
- retain durable deduplication, bounded delivery, and observed recovery using
  stable reason codes and task references; define operator acknowledgment
  separately from observed recovery before adding controls
- extend shipped dependency-review schedules to health summaries and evidence
  collection, defining one read-only purpose per implementation slice
- persist schedule identity, run history, next execution, final status, and
  artifacts through the existing task and Temporal model
- retain shipped read-only alert/schedule visibility; scope dashboard management
  controls separately through the existing API; defer natural-language requests

### Boundaries

- scheduled mutation remains disabled until separate evidence and explicit
  approval justify it
- alerts must not expose secrets, raw task text, or private artifact locations
- do not add a second scheduler or infrastructure dependency; use the existing
  durable task and Temporal boundaries
- no destructive automated recovery

### Exit criteria

- each M25.5 degraded scenario produces one actionable alert and one recovery
  transition without an alert storm
- notification failure is visible and retryable without blocking task-state
  reconciliation
- scheduled read-only runs are idempotent, inspectable, cancellable, and leave
  terminal evidence
- operators can determine what happened and the safe next action without
  starting with container logs

## Deferred milestone

### M27 — Reliability-Based Autonomy Policy

Reserved goal: allow selected low-risk work to move from blocking approval to
`proceed_with_flag` or `notify_only` when measured outcomes justify it.

Entry condition: M25.6 and the expanded M29 evidence provide sufficient
repeated samples, provenance, and reversible thresholds by repository and task
category, with M28.5 capability/evidence boundaries and later operational
evidence where relevant. The 20-task baseline alone is not sufficient.
High-risk categories remain blocking. M27 does not permit automatic merge or
deploy.

## Future / Conditional Execution Architecture

These are not committed near-term scope. Promote an idea only with a measured
problem, bounded design, rollback path, and explicit milestone.

### Durable Child Workflows / Agent Tree

Potential shape:

```text
TaskExecutionWorkflow
├── AgentChildWorkflow
├── AgentChildWorkflow
├── Verify
├── Review
└── Delivery
```

Temporal Child Workflows may fit truly independent, long-running, cancellable
agent tasks with separate histories. Do not map every small DAG node to a Child
Workflow; start with one workflow/agent until context or durability needs
justify the added coordination.

### Isolated Worktree / Patch Execution

Evaluate replacing shared mutable workspace assumptions with isolated
node/agent worktrees that produce patches or commits against a base revision,
followed by deterministic merge/reconciliation. Potential benefits include
retry safety, clearer ownership, easier rollback, and safer mutable parallelism.
Prove the replacement before removing the current node claim/lease mechanism.

### Durable Operator-Visible Agent Tree

A future dashboard could show Analyze, Implement, Verify/Repair, Independent
Review, and Delivery as a task tree. Each node could expose context,
runtime/provider, capability grant, duration, budget/cost, artifacts, patch,
retries, verification, and memory/skills used. This depends on stable event,
context, budget, and child-work contracts.

### Evidence-Backed Procedural Skills

Keep the distinction: memory records **what is known**; a skill records **how a
procedure is performed**. Repeated successful executions may produce a
`SkillCandidate`, but promotion requires supporting evidence, evaluation,
operator review, versioning, regression checks, and rollback.

A future `RepoSkill` could contain its trigger, prerequisites, procedure,
verification, required capabilities, failure modes, provenance, last verified
revision, and confidence. Agents must not install or activate self-created
trusted skills without evidence and review.

### Workspace Checkpoints / Rollback UX

Expose reversible checkpoints before/after mutation with patch/diff, verifier
results, and restore/retry controls, including an explicit retry with another
provider when safe. Prefer Git, worktrees, and patches over a bespoke snapshot
system when they satisfy the requirement.

### Long-Lived Temporal Workflow Lifecycle

Plan for compatible Temporal server/SDK upgrades, Worker Deployment Versioning,
workflow-code compatibility, Continue-As-New, history limits, old-workflow
drainage, and replay testing. Establish a lifecycle for current patch markers
rather than accumulating permanent compatibility branches indefinitely.

### Other Conditional Triggers

- remote sandbox scaling requires measured host saturation, contention, or
  concurrency pressure
- deep-scout / multi-phase research chaining remains conditional until simpler
  single-agent and bounded read-only fan-out approaches show a measurable
  limitation
- semantic retrieval requires an M28 miss that full-text/indexing/admission
  improvements cannot address
- new worker providers require M29 evidence for existing Codex/Antigravity
  profiles first

## Explicit Non-Goals and Deferred Actions

- no repository rewrite or architecture restart
- no replacement of Temporal with Hermes, OpenClaw, LangGraph, or another
  lifecycle engine
- no custom provider-independent reasoning loop
- no general multi-agent swarm or broad mutable fan-out
- no automatic merge or deploy
- no scheduler beside Temporal
- no vector database without M28 evidence
- no autonomous skill installation/activation
- no automatic reliability-based autonomy yet
- no broad provider expansion during M29
- no deletion of current reliability, state, claim/lease, signal/outbox, or
  compatibility mechanisms before a replacement is proven
- multi-user SaaS, billing, and broad RBAC remain outside personal v1

## Comparative References

These official/primary sources are design references, not proposed runtime
replacements. URLs were verified on 2026-08-13.

### Temporal

- [Workflow and durable execution model](https://docs.temporal.io/workflows)
- [Child Workflows](https://docs.temporal.io/child-workflows)
- [Python Queries, Signals, and Updates](https://docs.temporal.io/develop/python/workflows/message-passing)
- [Schedules](https://docs.temporal.io/schedule)
- [Continue-As-New](https://docs.temporal.io/workflow-execution/continue-as-new)
- [Python Workflow/Worker Versioning and replay](https://docs.temporal.io/develop/python/workflows/versioning)
- [Temporal Server releases](https://github.com/temporalio/temporal/releases)
- [Python SDK releases](https://github.com/temporalio/sdk-python/releases)

These references describe Temporal capabilities and compatibility mechanisms.
Our code-agent design default is to start with one Workflow and introduce Child
Workflows only when independent lifecycle, history, cancellation, scaling, or
isolation needs justify the additional coordination.

### Hermes Agent (Nous Research)

- [Delegation and isolated child contexts](https://hermes-agent.nousresearch.com/docs/guides/delegation-patterns/)
- [Durable Kanban versus process-local delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- [Memory, schedules, checkpoints, and subagents](https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/)
- [Procedural skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)

Hermes provides useful delegation, schedule UX, checkpoint, and
memory-versus-skill patterns. Its docs also state that ordinary top-level
delegation is process-local, so it is not a substitute for Temporal durability.

### OpenClaw

- [Subagent hierarchy, context, tool restrictions, and completion handling](https://github.com/openclaw/openclaw/blob/main/docs/tools/subagents.md)
- [Sandbox scopes](https://github.com/openclaw/openclaw/blob/main/docs/gateway/sandboxing.md)
- [Agent workspaces, memory, and skills](https://github.com/openclaw/openclaw/blob/main/docs/concepts/agent.md)

OpenClaw offers useful gateway/channel UX, session hierarchy, and sandbox/tool
scoping. It persists queued completion handoffs, but documents direct announce
attempts and cleanup timers as best-effort; that differs from making the full
child lifecycle a Temporal Workflow.

### OpenHands Software Agent SDK

- [Typed actions/observations, immutable events, and workspace abstraction](https://docs.openhands.dev/sdk/arch/sdk)
- [Architecture overview](https://docs.openhands.dev/sdk/arch/overview)

Its event and action/execution separation are useful references for
`AgentEvent` and the sandbox boundary. This roadmap does not propose replacing
provider-native cognition with the OpenHands reasoning loop.

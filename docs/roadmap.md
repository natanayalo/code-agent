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

## Current phase

Phase 4A: Temporal stabilization and measured reliability. M25.6 and M26 are
complete. M28 is active.

Committed/current priority:

1. finish M28: Memory Effectiveness and Session Continuity
2. M28.5: Execution Architecture Foundation
3. M29: Provider Reliability and Evidence-Driven Routing
4. M30: GitHub-Native Task and Delivery Control
5. M31: Proactive Operations and Safe Scheduled Work

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

- M20–M21 established the personal reliability baseline, task controls,
  TaskSpec contract, worker profiles, sandbox hardening, verification, review,
  delivery, and operator dashboard.
- M22 added an inspectable performance-routing policy. Its checked-in metrics
  are advisory seed data, not a live feedback loop from current task outcomes.
- M23 added skeptical personal/project memory, compact session state, full-text
  retrieval, admission review, observation evidence, and deterministic
  evaluation.
- M24 added persisted task decomposition and sequential DAG execution.
- M25 added durable node activities and opt-in bounded two-node read-only
  fan-out.
- M25.3 made Temporal the sole durable lifecycle runtime and retired the
  Postgres polling scheduler, LangGraph lifecycle, runtime selector, and lease
  schema. See the [Temporal cutover record](archive/temporal_cutover.md).
- M25.4 added patch-aware completion-loop parity for verifier and independent
  review repairs. Bounded repairs reuse the retained workspace and selected
  worker/permission path, repeat acceptance checks, and terminate in one
  actionable manual handoff when repair is unavailable or exhausted.
- M25.5 added dependency-aware readiness, stuck-work and terminal-reconciliation
  metrics, responsive dashboard status and recovery guidance, and current-branch
  real-worker lifecycle evidence.
- M25.6 established the 20-case real-worker Temporal reliability baseline and
  restored the 90% Python CI coverage gate.
- M26 added review-comment repair polling on open draft PRs with author filtering,
  budget capping, deduplicated reply planning, immediate per-reply DB checkpointing,
  and GitHub GraphQL thread replies.


## M25.6 — Real Temporal Reliability Baseline (Completed)

### Goal

Measure real provider and Temporal behavior before resuming review automation
or increasing autonomy.

This section preserves the historical goal and evidence contract. The reviewed
20-case baseline and 90% Python coverage restoration are complete; it did not
change routing automatically or authorize M27.

### Evidence set

Run 20 real-worker Temporal tasks:

- 4 read-only monolithic tasks
- 4 mutation tasks, including verifier and independent-review repair
- 3 sequential DAG tasks
- 2 opt-in read-only fan-out tasks
- 3 HITL tasks covering clarification, approval, and permission escalation
- 2 recovery tasks covering cancellation and worker restart
- 2 draft-PR delivery tasks

### Scope

- publish a persisted-evidence report covering interventions, repeated
  questions, manual-log inspection, validation evidence, provider failures,
  profile success, stage latency, time to terminal state or PR, and CI/review
  rejection
- require terminal reconciliation for every task, validation evidence for
  every completed mutation, and a typed failure plus next action for every
  failure
- keep performance-routing behavior unchanged; the report informs a later
  routing decision but does not automatically update production routing
- restore the Python CI coverage gate from the temporary 80% floor to the 90%
  repository target

### Exit criteria

- all 20 tasks have reviewable task, run, timeline, artifact, and Temporal
  evidence
- no task is silently stuck or terminally divergent
- completed mutation tasks have validation evidence and every failure is typed
  with an operator action
- Python CI enforces the 90% coverage target again
- the operator accepts a report that explicitly recommends whether M26 or M27
  is ready to resume

## M28 — Memory Effectiveness and Session Continuity

### Goal

Prove that durable memory and compact session state reduce repeated work
without allowing stale or weakly supported context to control execution.

### Scope

- add a paired evaluation set for repeated tasks in the same repository,
  covering a useful memory hit, irrelevant-memory rejection, stale-memory
  re-verification, and conflicting-memory handling
- capture which memory candidates were retrieved, admitted, verified, applied,
  or rejected, with operator-visible reasons
- populate `decisions_made` and `identified_risks` from typed task and worker
  outcomes instead of leaving those compact-session fields mostly empty
- verify that active goals, decisions, risks, and touched files survive task
  replay and session resume without becoming opaque prompt blobs
- compare current full-text retrieval against the observed misses before
  considering semantic retrieval infrastructure

M28 is complete. The paired evaluation baseline, typed compact-session
continuity, and reviewed eight-pair real-worker matrix are implemented. The
matrix is effective across both native read-only profiles: useful project
memory reaches and influences the worker where expected, while irrelevant,
stale, and conflicting memory is rejected or suppressed. Questions and
interventions did not regress; completion times were mixed, so this is not a
general timing-improvement claim. The typed compact session fields remain
useful and can later feed `ContextEnvelope`.

### Boundaries

- retain the existing personal, project, and session-state categories
- keep memory inspectable, editable, deletable, scoped, and skeptical
- do not add a vector database unless the paired evaluation demonstrates a
  material retrieval failure that full-text improvements cannot address
- do not add learned skills, large dependencies, or an execution-architecture
  rewrite unless measured M28 evidence demonstrates a need
- do not store autonomous approval, deployment, or destructive-action policy
  in learned memory

### Exit criteria

- a reviewed report states whether memory reduces repeated questions,
  interventions, or time to a validated result on the paired cases
- every applied memory has provenance, confidence, scope, verification state,
  and an operator-visible reason for use
- stale or conflicting memory is re-verified or rejected before it affects a
  worker request
- decisions and risks persist across a resumed session and are visible through
  the API and dashboard

## M28.5 — Execution Architecture Foundation

### Goal

Strengthen the execution trust boundary and establish stable contracts for
future provider, orchestration, and observability work without rewriting the
proven Temporal lifecycle.

This is a bounded foundation milestone delivered in small compatible slices,
not a broad repo or workflow rewrite.

### M28.5A — Sandbox / Trust-Boundary Hardening

The completed M28 live matrix ran through the native-agent executor boundary.
Continue this milestone's remaining architecture work independently of the
completed M28 evidence.

Current concern: the Temporal worker mounts the host Docker socket, launches
native provider execution, mounts Codex/Antigravity authentication, and may
select trusted Codex `danger-full-access` inside the current worker-controlled
container topology. This concentrates container-runtime authority, provider
credentials, and native execution too closely.

Implemented direction for this slice:

```text
Trusted Temporal worker (Docker authority)
        ↓
one-shot per-task Docker executor + task-private HTTPS proxy
        ↓
Codex / Antigravity native CLI
```

Only trusted sandbox infrastructure should need container-runtime authority. Native
agents must not access the container-control interface, unrelated host
resources, or broad infrastructure credentials. Define the threat boundary
before choosing a dedicated broker, rootless Docker, user namespaces,
containerd/runtime abstraction, remote sandbox service, or future remote
execution.

Workflow/orchestration contracts should prefer opaque `SecretRef` or capability
references instead of raw credentials. The sandbox broker/runtime resolves and
injects only the credential required by the grant, just in time and for the
narrowest practical process and lifetime. Secrets should not unnecessarily
enter Temporal history, `AgentEvent`, persisted `ContextEnvelope`, logs,
artifacts, or general worker-request payloads. The exact mechanism remains an
M28.5A design decision.

Exit criteria:

- documented threat model across control plane, sandbox infrastructure,
  native process, workspace, network, provider auth, and artifact paths
- native agent cannot use the container-control interface or host socket
- credentials follow least-privilege exposure
- execution contracts and persisted evidence avoid raw secret propagation
- existing supported task reliability still passes focused and end-to-end
  verification
- no increase in autonomous privileges

### M28.5B — State Ownership Contract

Current lifecycle information overlaps across Temporal workflow/history state,
serialized `TemporalTaskState` / `OrchestratorState`, task/product tables,
execution-plan/node-attempt rows, and timeline/event projections.

Target ownership:

- **Temporal**: lifecycle/control truth, current workflow decisions, waits,
  retries, cancellation, durable coordination, and future schedules/child work
- **Postgres**: product/query projections, memory, evaluations, artifact
  metadata, external GitHub/channel identities, operator/search/reporting data,
  and external-side-effect idempotency where needed

Do not immediately remove current persistence. First document field-level
authority, recovery/projection rules, and compatibility. The field-level state
ownership contract, recovery rules, and prioritized reduction plan are
established in [`docs/architecture/state_ownership.md`](architecture/state_ownership.md).
New features must not deepen duplicate lifecycle ownership. Reduce full-state
`TemporalTaskState` duplication incrementally across planned waves only when replay,
recovery, query, and rollback behavior remain proven.

### M28.5C — Provider-Neutral `AgentEvent`

Keep `WorkerRequest -> WorkerResult` compatibility while defining a versioned
event direction:

- `AgentStarted`, `AgentProgress`, and `AgentMessage`
- `ToolRequested` and `ToolCompleted`
- `FileChanged`, `PermissionRequested`, and `ArtifactProduced`
- `BudgetUpdated`
- `AgentFailed` and `AgentCompleted`

Adapters should normalize Codex/Antigravity native streams into these events.
`WorkerResult` remains a terminal projection during migration. The stream
supports progress, audit, stuck detection, cancellation, memory evidence,
budget/cost accounting, debugging, and reliability evaluation; it must not
become a custom provider-independent reasoning loop.

### M28.5D — Versioned `ContextEnvelope`

Design a bounded, inspectable, reproducible context contract containing:

- objective and acceptance criteria
- relevant repository facts and selected file/context references
- dependency outputs
- accepted/gated memory with provenance
- compact session decisions and known risks
- applicable repository skills/instructions
- capability summary and explicit exclusions

Persist or reference the envelope as execution evidence. Memory remains
advisory. M28 typed compact-session state is an input; copying the entire parent
conversation is not a `ContextEnvelope`.

### Incremental Contract Direction

Current TaskSpec remains compatible while the architecture evolves toward:

| Contract | Responsibility |
| --- | --- |
| `IntentSpec` | Goal, acceptance criteria, assumptions, non-goals |
| `ContextEnvelope` | Repository/session/memory/dependency context |
| `ExecutionPlan` | Nodes, dependencies, expected outputs |
| `CapabilityGrant` | Deterministically generated read/write/shell/network/Git/GitHub/secret access |
| `VerificationPlan` | Deterministic checks and required evidence |
| `DeliverySpec` | Summary/workspace/branch/draft-PR result |
| `BudgetSpec` | Time, cost/tokens, attempts, child/concurrency, repair limits |

M28.5 establishes stable seams; it need not finish this split or require an
immediate breaking migration.

## M29 — Provider Reliability and Evidence-Driven Routing

### Goal

Turn real task outcomes into current, explainable worker-profile recommendations
without silently changing production routing or weakening safety boundaries.

Routing should evolve conceptually from named/static profiles toward:

```text
task requirements
    ∩
runtime capabilities
    ∩
measured reliability
    ∩
budget
    ↓
route decision
```

### Scope

- define minimum sample counts by task class and worker profile before
  collecting the expanded post-M25.6 evidence set
- measure completion, typed failure, repair, intervention, latency, and budget
  outcomes for the current Codex and Antigravity native profiles
- define and measure capabilities such as native structured output, event
  streaming, sandbox/read-only enforcement, session resume, network control,
  tool audit quality, context limits, cost visibility, and delivery support
- distinguish agent execution, acceptance, verification, review, and delivery
  outcomes in the reliability evidence
- add operator diagnostics for provider authentication, CLI availability,
  sandbox readiness, and other last-mile capability failures that a fresh
  worker heartbeat cannot prove
- replace checked-in seed metrics with a versioned advisory report generated
  from persisted, reviewable evidence
- show sample size, recency, confidence, and fallback reason with every routing
  recommendation
- preserve manual profile overrides and a reversible path back to the current
  static routing policy
- evaluate hierarchical budgets for the whole task, node/child, repair,
  maximum worker calls, wall time, and concurrency

### Boundaries

- do not add another provider during this milestone
- do not automatically update routing from evaluation output
- do not silently retry mutable work on a different provider after partial
  execution
- keep provider diagnostics separate from global process liveness

### Exit criteria

- every enabled profile has current evidence or is explicitly labeled
  insufficient-data
- unavailable provider capabilities fail before or during dispatch with a
  typed reason and actionable operator guidance
- the operator can explain and reproduce every recommended profile choice from
  persisted evidence
- any routing-policy change is reviewed, versioned, reversible, and validated
  against a held-out task set

## M30 — GitHub-Native Task and Delivery Control

### Goal

Reduce manual translation between GitHub work items and code-agent tasks while
keeping task creation, repair, merge, and deployment decisions explicit.

### Scope

- add authenticated, idempotent GitHub event intake for explicitly selected
  issue and delivery events
- persist the external issue, branch, commit, pull-request, and check-run
  identity on the existing task and delivery timeline
- allow an operator to start or replay a task from an eligible issue event
  without copying the request into a generic webhook payload
- synchronize pull-request and CI state into the dashboard operator inbox with
  direct links and safe next actions
- preserve one traceable chain from source issue through task, worker evidence,
  validation, branch, and draft PR
- evaluate Temporal-native interaction primitives where they simplify real
  M30 requirements: Queries for status/current plan/pending decision; Updates
  for clarify, approve/reject, capability grant, node retry, budget increase,
  or worker switch; and Signals for asynchronous external events

### Boundaries

- require an explicit assignment, command, or operator action before creating
  a mutable task
- do not rewrite the proven signal/outbox or completed M26 review-comment path
  solely for architectural purity; migrate only where an M30 flow becomes
  simpler without losing reliability
- no auto-merge, auto-deploy, broad repository write scope, or new secret scope
- keep GitHub as the only native source-control integration in this slice

### Exit criteria

- one issue-to-task-to-draft-PR flow completes with linked evidence at every
  boundary
- duplicate or reordered GitHub deliveries do not create duplicate tasks or
  timeline events
- CI and pull-request state are visible without initial log inspection
- every follow-up action remains explicit, authenticated, and auditable

## M31 — Proactive Operations and Safe Scheduled Work

### Goal

Move from dashboard-only detection to actionable notification and repeatable
read-only maintenance without introducing unattended mutation risk.

Natural-language schedule requests resolve through one lifecycle:

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

This is a bounded architecture/operations prerequisite, not an instruction to
upgrade blindly to the latest release. M31 scheduling work starts only after
the selected server/SDK combination and workflow-evolution policy are proven.

### Scope

- notify through Telegram and generic webhooks when readiness, outbox age,
  worker freshness, stuck waits, terminal divergence, or provider capability
  crosses a configured degraded threshold
- deduplicate, throttle, acknowledge, and resolve alerts using stable reason
  codes and task references
- add scheduled read-only tasks for health summaries, evidence collection,
  dependency review, and other operator-approved maintenance
- persist schedule identity, run history, next execution, final status, and
  artifacts through the existing task and Temporal model
- show alerts and scheduled-run history in the dashboard with safe recovery
  guidance

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

## Completed and Deferred Milestones

### M26 — Review Comment Repair (Completed)

Goal: extend the draft-PR repair loop from CI failures to actionable GitHub
review comments with author filtering, budget capping, deduplicated reply planning,
immediate per-reply DB checkpointing, and thread replies. Completed in Phase 4A.

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

## Open Planning Questions

1. What M28 paired-result threshold supports an effectiveness claim, and what
   measured miss would justify semantic retrieval?
2. Which M28.5A threat assumptions differ for local and future remote sandbox
   execution?
3. Which `AgentEvent` subset can both native providers expose without lossy
   inference?
4. What minimum sample size/recency by task class and capability should M29
   require?
5. Which M30 interactions materially benefit from Temporal Updates rather than
   the proven signal/outbox path?
6. Which read-only schedule and degraded thresholds form the first M31 slice?

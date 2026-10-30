# Completed milestone specifications

Archived from the roadmap on 2026-10-30. These are the original bounded scopes,
contracts, and exit criteria for completed work; imperative wording describes
historical plans and does not create new tasks. Relative status references refer
to the record at closeout. Current runtime contracts remain in the architecture,
state-ownership, and Temporal compatibility documents.

Use [current status](../status.md#current-backlog) to select work and the
[roadmap](../roadmap.md) for active/deferred/future scope. Qualification evidence
is in [Phase 4A evidence](phase4a_evidence.md), with merged history in
[CHANGELOG.md](../../CHANGELOG.md).

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
  schema. See the [Temporal cutover record](temporal_cutover.md).
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

## M28 — Memory Effectiveness and Session Continuity (Completed)

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

## M28.5 — Execution Architecture Foundation (Completed)

### Goal

Strengthen the execution trust boundary and establish stable contracts for
future provider, orchestration, and observability work without rewriting the
proven Temporal lifecycle.

This is a bounded foundation milestone delivered in small compatible slices,
not a broad repo or workflow rewrite.

### M28.5A — Sandbox / Trust-Boundary Hardening

The completed M28 live matrix ran through the native-agent executor boundary.
M28.5 subsequently completed its bounded architecture foundation; the
remaining conditional architecture ideas are listed separately below.

Original concern: the Temporal worker mounts the host Docker socket, launches
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
artifacts, or general worker-request payloads.

M28.5A and its Docker runtime enforcement (M28.5A.2) are now complete.

Exit criteria (completed):

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

M28.5B and its defined Waves 1–3 are complete. The field-level state ownership
contract, recovery rules, completed reductions, and retained-state rationale are
recorded in
[`docs/architecture/state_ownership.md`](../architecture/state_ownership.md). New
features must not deepen duplicate lifecycle ownership. Any further reduction
of `TemporalTaskState` requires proven replay, recovery, query, and rollback
behavior.

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

### M28.5D — Versioned `ContextEnvelope` [COMPLETED]

Designed and implemented a bounded, inspectable, reproducible context contract containing:

- objective and acceptance criteria
- relevant repository facts and selected file/context references
- dependency outputs
- accepted/gated memory with provenance
- compact session decisions and known risks
- applicable repository skills/instructions
- capability summary and explicit exclusions

Assembled immediately prior to dispatch and passed via `WorkerRequest.context_envelope`.
Persisted as execution evidence inline in `artifact_metadata` on an `ArtifactReference`
of type `context_envelope` attached to `WorkerResult.artifacts`. Features dual digests
(`context_content_digest` for reproducibility, `evidence_digest` for audit lineage),
progressive string and list truncation with explicit `truncation_records`, secret
redaction via `SecretRedactor`, and a full operator dashboard inspection panel.

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

## M29 — Provider Reliability and Evidence-Driven Routing (Completed)

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
- complete the compatibility-gated removal of `LegacyIngressTaskRequest` and
  raw secret ingress; reject unregistered or raw secret payloads fail-closed
  without broadening credential scope
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

### Closeout and evidence follow-up

M29 engineering and its expanded evaluation are complete. At closeout all four
canonical cells met the 10-task minimum per compatible provider:

| Task class / mode | Advisory recommendation |
| --- | --- |
| docs / read_only | Codex |
| feature / read_only | Antigravity |
| investigation / read_only | Antigravity |
| feature / mutation | Codex |

Recommendations describe the evaluated cohorts, not an automatic runtime policy.
Feature/read-only acceptance is terminal completion, not an independently
verified quality claim. Model-aware longitudinal robustness remained partial
at the Wave 5 timestamp, `2026-10-26T09:12:04.572097Z`; eligibility must be
recomputed from actual cohort timestamps and sample counts.

The [evidence archive](phase4a_evidence.md) preserves wave accounting,
identity exclusions, quality results, and pinned revisions. The
[evidence-gated backlog](../status.md#evidence-gated-follow-up) tracks the later
report refresh. Production routing remains static and M27 stays deferred.

### Exit criteria

- every enabled profile has current evidence or is explicitly labeled
  insufficient-data (satisfied: `docs/read_only`, `feature/read_only`,
  `investigation/read_only`, and `feature/mutation` all have verified empirical advisory evidence)
- unavailable provider capabilities fail before or during dispatch with a
  typed reason and actionable operator guidance (satisfied: M29 Items 1a/1b implement
  registered-reference gating and provider pre-dispatch diagnostics)
- the operator can explain and reproduce every recommended profile choice from
  persisted evidence (satisfied: reproducible via offline canonical report CLI, sanitized manifests, and verified artifacts)
- any routing-policy change is reviewed, versioned, reversible, and validated
  against a held-out task set (satisfied: production routing remains static; future routing policy updates require explicit change controls and validation)

M29 and M30 are complete. M31 is the current milestone; its ordered slices
are maintained in [status](../status.md#current-backlog).

## M30 — GitHub-Native Task and Delivery Control (Completed)

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

### M26 — Review Comment Repair (Completed)

Goal: extend the draft-PR repair loop from CI failures to actionable GitHub
review comments with author filtering, budget capping, deduplicated reply planning,
immediate per-reply DB checkpointing, and thread replies. Completed in Phase 4A.

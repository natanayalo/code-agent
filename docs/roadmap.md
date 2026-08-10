# Roadmap

## Planning principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage from Codex, Antigravity, and OpenRouter over
  rebuilding provider-local cognition
- keep trust-boundary and high-risk changes human-controlled
- require measured real-worker evidence before increasing autonomy

## Current phase

Phase 4A: Temporal stabilization and measured reliability.

Priority sequence:

1. M25.6: Real Temporal Reliability Baseline
2. M28: Memory Effectiveness and Session Continuity
3. M29: Provider Reliability and Evidence-Driven Routing
4. M30: GitHub-Native Task and Delivery Control
5. M31: Proactive Operations and Safe Scheduled Work

M26 review-comment repair and M27 reliability-based autonomy remain reserved
but explicitly deferred. Neither milestone resumes until the M25.6 evidence
review concludes that its entry conditions are satisfied.

Milestone numbers are stable identifiers; the priority sequence above is the
authoritative execution order. M26 may be promoted into that sequence only by
an explicit post-M25.6 decision. M27 requires broader repeated evidence from
M29 and cannot resume from the 20-task baseline alone.

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


## M25.6 — Real Temporal Reliability Baseline

### Goal

Measure real provider and Temporal behavior before resuming review automation
or increasing autonomy.

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

### Boundaries

- retain the existing personal, project, and session-state categories
- keep memory inspectable, editable, deletable, scoped, and skeptical
- do not add a vector database unless the paired evaluation demonstrates a
  material retrieval failure that full-text improvements cannot address
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

## M29 — Provider Reliability and Evidence-Driven Routing

### Goal

Turn real task outcomes into current, explainable worker-profile recommendations
without silently changing production routing or weakening safety boundaries.

### Scope

- define minimum sample counts by task class and worker profile before
  collecting the expanded post-M25.6 evidence set
- measure completion, typed failure, repair, intervention, latency, and budget
  outcomes for the current Codex and Antigravity native profiles
- add operator diagnostics for provider authentication, CLI availability,
  sandbox readiness, and other last-mile capability failures that a fresh
  worker heartbeat cannot prove
- replace checked-in seed metrics with a versioned advisory report generated
  from persisted, reviewable evidence
- show sample size, recency, confidence, and fallback reason with every routing
  recommendation
- preserve manual profile overrides and a reversible path back to the current
  static routing policy

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

### Boundaries

- require an explicit assignment, command, or operator action before creating
  a mutable task
- keep automatic review-comment repair in the deferred M26 milestone
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

## Deferred milestones

### M26 — Review Comment Repair (Completed)

Goal: extend the draft-PR repair loop from CI failures to actionable GitHub
review comments with author filtering, budget capping, deduplicated reply planning,
immediate per-reply DB checkpointing, and thread replies. Completed in Phase 4A.

### M27 — Reliability-Based Autonomy Policy

Reserved goal: allow selected low-risk work to move from blocking approval to
`proceed_with_flag` or `notify_only` when measured outcomes justify it.

Entry condition: M25.6 and the expanded M29 evidence provide sufficient
repeated samples, provenance, and reversible thresholds by repository and task
category. The 20-task baseline alone is not sufficient. High-risk categories
remain blocking.

## Conditional backlog

- remote sandbox scaling becomes a milestone only after measured host
  saturation, workspace contention, or required concurrency exceeds the local
  Docker model
- semantic memory retrieval becomes a milestone only if M28 proves a material
  miss that cannot be addressed with full-text indexing, query normalization,
  or better admission metadata
- broader mutable fan-out, platform-managed multi-agent workflows, and
  deep-scout chaining remain deferred until sequential execution and the
  read-only pilot show measurable benefit
- new worker providers remain deferred until M29 establishes reliable evidence
  for the profiles already supported
- multi-user SaaS, billing, RBAC, auto-merge, and auto-deploy remain outside the
  personal v1 product boundary

## Open planning questions

1. What minimum sample count by task class and worker profile should M29 require
   before a routing recommendation is considered current?
2. Which GitHub issue events may create tasks directly in M30, and which must
   remain operator-confirmed?
3. Which notification channels and degraded thresholds are required for the
   first M31 slice?
4. What measured improvement in the M28 paired evaluation would justify adding
   semantic retrieval rather than improving full-text behavior?
5. If M25.6 makes M26 eligible, should it be promoted ahead of M28 or remain
   deferred until the committed M28-M31 sequence is complete?

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

1. M25.4: Temporal Core Completion-Loop Parity
2. M25.5: Truthful Readiness and Operator Recovery
3. M25.6: Real Temporal Reliability Baseline

M26 review-comment repair and M27 reliability-based autonomy remain reserved
but explicitly deferred. Neither milestone resumes until the M25.6 evidence
review concludes that its entry conditions are satisfied.

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

## M25.4 — Temporal Core Completion-Loop Parity

### Goal

Ensure every core verifier or independent-review repair decision has a durable
Temporal continuation instead of ending after the first verification pass.

### Scope

- make verifier and review activities return an explicit continuation decision
  to the workflow
- on a bounded repair request, reprovision the retained workspace as needed,
  rerun the selected worker with the persisted repair instructions, and repeat
  verification
- preserve existing repair budgets, permission escalation, cancellation,
  activity idempotency, terminal projection, and history replay compatibility
- end in an actionable manual-follow-up state when repair is rejected,
  non-repairable, or exhausted
- add Temporal integration and focused E2E coverage for successful repair,
  exhausted repair, worker restart during repair, duplicate activity delivery,
  and history replay

### Boundaries

- bounded two-node read-only fan-out remains opt-in
- deep-scout repo-to-research chaining is explicitly deferred
- no new provider, deployment, or autonomy policy is introduced

### Exit criteria

- no core repair flag can be persisted without either executing the repair or
  producing an explicit terminal/manual-follow-up state
- verifier and independent-review repairs each complete successfully through a
  Temporal integration test
- repair exhaustion, cancellation, restart recovery, and replay leave one
  consistent Postgres projection and timeline
- the focused operator-flow E2E passes on the production-like Compose stack

## M25.5 — Truthful Readiness and Operator Recovery

### Goal

Make every execution-blocking dependency and stuck-work condition visible
without requiring initial log inspection.

### Scope

- retain `/health` as process liveness and make `/ready` report dependency
  readiness for Postgres, Temporal, command dispatch, and fresh worker
  availability
- expose pending, retrying, and dead-letter command counts, oldest outbox age,
  worker heartbeat, stuck interaction waits, and Temporal/Postgres terminal
  divergence through machine-readable metrics
- add a minimal dashboard status view with dependency state, degraded reasons,
  and safe operator recovery guidance
- keep reads and interactions available during Temporal degradation while new
  submissions continue to fail visibly
- verify database outage, Temporal outage and recovery, missing worker,
  dispatcher backlog, stuck interaction, and terminal reconciliation behavior

### Exit criteria

- `/ready` becomes non-ready for each execution-blocking dependency failure and
  recovers without an API restart when the dependency returns
- every monitored degraded state is visible through both API/metrics and the
  dashboard
- one current-branch smoke proves API → outbox → Temporal → worker → Postgres
  completion
- the runbook provides a safe recovery action for every surfaced state

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

## Deferred milestones

### M26 — Review Comment Repair

Reserved goal: extend the draft-PR repair loop from CI failures to actionable
GitHub review comments.

Entry condition: CI repair and the core Temporal repair loop are stable in the
M25.6 real-worker evidence set. No auto-merge or deploy behavior is included.

### M27 — Reliability-Based Autonomy Policy

Reserved goal: allow selected low-risk work to move from blocking approval to
`proceed_with_flag` or `notify_only` when measured outcomes justify it.

Entry condition: M25.6 provides sufficient real-task samples, provenance, and
reversible thresholds by repository and task category. High-risk categories
remain blocking.

## Open planning questions

1. What minimum sample count by task class and worker profile is sufficient to
   replace the advisory routing metrics with measured outcomes?
2. Which M25.6 outcomes are strong enough to resume M26 without expanding its
   scope beyond draft PRs?
3. Which low-risk categories, if any, have enough evidence to begin an M27
   policy experiment?
4. When does the accepted memory corpus become large enough to justify an
   FTS-versus-semantic retrieval comparison?

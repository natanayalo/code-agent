# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Antigravity/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 4: selective autonomy after reliability.

Priority sequence:

1. M25.3: Temporal-Only Cutover and Legacy Retirement
2. Milestone 26: Review Comment Repair
3. Milestone 27: Reliability-Based Autonomy Policy

Completed foundation:

1. Phase 4 early milestones: M22 (Eval-driven routing), M23 (Memory admission & retrieval), M24 (Decomposed Task DAG), M25 (Parallel worker fan-out)
2. Phase 3: Personal reliability before broader autonomy (Milestones 20 through 21)

Past phases:

1. Phase 2: bounded autonomy (Milestones 18 through 19.5)
2. Phase 1: clarity and control (Milestones 15 through 17.5)

## Phase 3 North Star

Reduce babysitting for personal coding tasks before expanding broader autonomy.

Primary success metrics:

- human interventions per completed task
- repeated questions per task
- tasks requiring manual log inspection
- validation evidence rate
- worker/provider failure rate
- success rate by worker profile
- latency by orchestrator stage
- time from task submission to terminal state or draft PR
- CI/review rejection rate for generated PRs

Phase 3 keeps the product personal-use first. It explicitly does not optimize for:

- multi-user/team SaaS workflows, tenancy, billing, roles, or organization administration
- auto-merge or auto-deploy
- broad multi-agent swarms
- new model/provider expansion before reliability improves
- autonomous privileged maintenance

### M25.3 Temporal-Only Cutover and Legacy Retirement

Goal:

- retire the legacy Postgres-polling scheduler and LangGraph durable lifecycle,
  making Temporal the sole durable execution orchestrator

Boundary:

- Temporal becomes the only durable execution scheduler and lifecycle engine
- Postgres retains tasks, interactions, timelines, worker evidence, artifacts,
  memory, delivery metadata, and dashboard queries
- code-agent retains planning, decomposition, routing policy, worker/provider
  behavior, sandbox policy, validation, review, and memory governance
- no new Temporal feature development — this is legacy retirement and operational cutover

Design decisions:

- persist `orchestration_runtime` on both `Task` (authoritative for drain metrics)
  and `WorkerRun` (execution evidence), using a portable constrained
  `OrchestrationRuntime` enum with values `temporal` and `legacy`
- runtime is pinned to the task at submission and immutable — no re-evaluation per run
- historical backfill uses conservative classification: positively identified rows
  get their runtime; ambiguous rows stay `NULL` (displayed as "unknown")
- worker fail-fast: bounded connection retries, then exit non-zero
- API graceful degradation: remains available for reads/dashboard/interactions,
  returns 503 for new submissions when Temporal is unreachable, no automatic
  fallback to legacy
- flat evidence gate before legacy deletion: all 14 operational scenarios, all
  10 task classes, required automated suites, last-known-good rollback image,
  a runtime-drain snapshot with zero active legacy/unknown tasks and zero
  post-cutover legacy submissions, and operator sign-off; scenarios 9 through
  12 may cite passing integration tests instead of manual Compose execution
- persisted cutover timestamp (`TEMPORAL_ONLY_CUTOVER_AT`) for drain queries
  instead of rolling windows
- legacy deletion and schema cleanup are two PRs: one code-deletion PR for
  dispatch, LangGraph lifecycle, and configuration; one schema-migration PR
- `graph.py` retains reusable domain nodes; only LangGraph lifecycle is removed
- rollback via last-known-good image + configuration + schema compatibility
  runbook, not git revert alone

Progress:

- [x] Slice 1: runtime observability
  - add `OrchestrationRuntime` enum and `orchestration_runtime` to Task and WorkerRun
  - conservative nullable backfill for historical rows
  - centralize WorkerRun creation to propagate the runtime marker
  - pin runtime to task at submission (immutable after creation)
  - dashboard drain-gate widgets: tasks by runtime and active legacy count;
    defer legacy submissions since cutover until Slice 2 persists the cutover timestamp
  - deployment prerequisite: deploy with zero active tasks, or explicitly classify,
    complete, or cancel every active unknown task before relying on the scheduler boundary
  - fix status.md Active Focus, add M25.3 to roadmap
- [x] Slice 2: production cutover
  - default `execution_runtime()` to `temporal` when unconfigured
  - remove `CODE_AGENT_USE_TEMPORAL` env var support
  - worker fail-fast: bounded Temporal connection retries, then exit non-zero
  - API graceful degradation: 503 for new submissions, reads stay available,
    ongoing Temporal readiness check
  - persist `TEMPORAL_ONLY_CUTOVER_AT` cutover timestamp
  - document all 14 operational evidence scenarios in
    `docs/m25_3_temporal_cutover_verification.md`; recording their Compose
    results is the entry gate for Slice 3
- [x] Slice 3A: local Compose rehearsal
  - record all 14 operational scenarios; scenarios 9 through 12 may cite
    passing integration-test evidence instead of manual Compose execution
  - record full task-class coverage (simple read-only, mutable implementation,
    sequential DAG, fan-out DAG, approval, clarification, permission
    escalation, cancellation, provider retry or restart, terminal failure); a
    single task may cover multiple classes
  - record passing unit, integration, pre-commit, and dashboard coverage suites
    and obtain local operator sign-off
  - completed by operator acceptance after local Compose rehearsal
- [x] Slice 3B: immutable release evidence gate
  - capture a release-environment clean runtime-drain snapshot, cutover timestamp, and
    immutable deployment-specific evidence ledger
  - tag and retain a last-known-good legacy-capable rollback artifact
  - obtain operator approval before legacy deletion and schema cleanup
  - completed against the sole local Compose release environment at commit
    `251b9aa`; the worker-restart fan-out recovery proof, image digests, clean
    drain, rollback tags, and operator approval are recorded in the immutable
    `m25.3-temporal-cutover-20260726T213001Z` release
- [ ] Slice 4: legacy deletion and schema cleanup (two PRs)
  - [x] PR 4A — remove legacy dispatch, LangGraph durable lifecycle, and runtime
    selector/configuration after a reference inventory and method-level
    WorkerNode audit; retain historical runtime evidence and schema compatibility
  - PR 4B — remove `lease_owner`, `lease_expires_at`, and `next_attempt_at`
    through a schema migration after PR 4A; the PRs may merge on the same day
    only after the schema rollback procedure is verified
  - pre-implementation reference inventory of all legacy symbols classified as
    legacy-only, shared product policy, test fixture, or migration compatibility
  - method-level WorkerNode audit: keep profile/capability/health/operator policy,
    remove only claim/lease/reclaim mechanics
  - retain `attempt_count`, `max_attempts`, `priority`, and `queue_lane`
  - before PR 4B, take a restorable database snapshot and verify the migration's
    upgrade, downgrade, and re-upgrade on a disposable database; record the
    exact tagged image plus database restore sequence
  - if PR 4B must be rolled back, stop the application, restore the pre-PR-4B
    database snapshot, deploy the tagged legacy-capable image and matching
    configuration, then verify the restored schema revision before resuming

Operational evidence scenarios (documented before Slice 3):

1. authenticated Compose execution (full task lifecycle)
2. approval, clarification, and permission resume via Temporal signals
3. cancellation while a provider worker is running
4. worker restart during an Activity (Temporal retries/recovers)
5. Temporal server/worker outage behavior (graceful degradation)
6. sequential DAG execution
7. two-node fan-out execution
8. replay of older Temporal workflow histories
9. full Python test suite and pre-commit pass
10. API/worker configuration mismatch fails visibly (not silent fallback)
11. Temporal unavailable while API remains inspectable (503 for submissions)
12. Temporal recovers after API has rejected submissions (tasks resume)
13. workflow and Postgres terminal states reconcile after worker restart
14. existing M25.1/M25.2 workflow histories replay after deployment

Task field disposition after retirement:

| Field              | Action | Reason                                     |
| ------------------ | ------ | ------------------------------------------ |
| `lease_owner`      | remove | legacy scheduler only                      |
| `lease_expires_at` | remove | legacy scheduler only                      |
| `next_attempt_at`  | remove | Temporal handles retry scheduling          |
| `attempt_count`    | keep   | product-level logical attempt evidence     |
| `max_attempts`     | keep   | product-level policy                       |
| `priority`         | keep   | routing policy, useful without PG dispatch |
| `queue_lane`       | keep   | routing policy for multi-queue scenarios   |

### M26 Review Comment Repair

Goal:

- extend the PR repair loop from CI failures to review-comment fixes

Scope:

- ingest actionable GitHub PR review comments
- create focused repair tasks linked to the original PR and comment thread
- preserve existing no-auto-merge and no-deploy boundaries

Boundary:

- start only after M20.6 CI repair is stable

### M27 Reliability-Based Autonomy Policy

Goal:

- let low-risk work move from blocking approval toward `proceed_with_flag` or `notify_only`
  when measured outcomes support it

Scope:

- define risk/category thresholds using M20/M22 metrics
- keep high-risk categories blocking
- show autonomy policy decisions in task timelines and dashboard

Boundary:

- autonomy increases are reversible and scoped by repo/category

## Phase Sequencing Summary

Phase 1:

1. Milestone 15
2. Milestone A
3. Milestone 16
4. Milestone 17

Phase 2:

1. Milestone 18
2. Milestone 19
3. Milestone 19.5

Phase 3:

1. M20.0 [x]
2. Milestone 20 [x]
3. Milestone 21 [x]

Phase 4:

1. Milestone 22 [x]
2. Milestone 23 [x]
3. Milestone 24 [x]
4. Milestone 25 (fan-out) [x]
5. M25.3 (Temporal cutover and legacy retirement)
6. Milestone 26
7. Milestone 27

## Open Planning Questions

1. which public product sentence remains canonical after milestone A rollout?
2. which runtime owns planning by default in production policy?
3. should persisted runtime manifests start as artifact-index entries or move directly to a queryable JSON column?
4. which low/medium-risk decision categories are safe to remember per repo?
5. which worker runtimes can provide reliable cancellation evidence in v1?
6. when should CI repair expand beyond GitHub draft PRs?
7. what metric threshold is good enough to promote a category from blocking approval to proceed-with-flag?
8. which memory misses justify adding semantic retrieval infrastructure?
9. which task classes benefit enough from DAG/parallel execution to justify the added complexity?

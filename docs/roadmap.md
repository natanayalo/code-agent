# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Antigravity/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 4: selective autonomy after reliability.

Priority sequence:

1. Milestone 24: Decomposed Task DAG
2. Milestone 25: Parallel Worker Fan-Out
3. Milestone 26: Review Comment Repair
4. Milestone 27: Reliability-Based Autonomy Policy

Completed foundation:

1. Phase 3: Personal reliability before broader autonomy

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

## M20.0: Baseline Eval Harness

Goal:

- establish a small measurement baseline before changing reliability contracts

Scope:

- define 8 to 12 representative real-project tasks
- cover bugfix, feature, refactor, review-fix, scout/investigation, and blocked-decision cases
- record whether each task completed, needed human decisions, repeated a question,
  produced validation evidence, failed due to worker/runtime issues, and reached a usable result
- capture elapsed time to terminal state
- break results down by worker profile, orchestrator stage, decision type, validation failure type,
  and whether manual log inspection was needed

Acceptance criteria:

- baseline tasks are runnable through the existing task API or a thin local harness
- results are stored in a simple inspectable artifact, not a hidden benchmark service
- baseline metrics are documented before judging M20.1 through M20.6 improvements

## Milestone 20: Personal Reliability Program

Goal:

- reduce operator babysitting by making runtime identity, constraints, decisions,
  validation, and maintenance paths explicit to workers/operators

Planned deliverables:

- runtime operating contract (identity/build/runtime/worker/tool/approval capabilities)
- agent-visible request-only maintenance actions (restart, recycle worker, reload config, dependency refresh, operator attention)
- observable ExecutionPlan spine for complex tasks
- decision-card inbox for resumable human input
- worker supervisor and health registry
- repo validation profiles
- draft PR / CI repair loop
- expanded internal complex-task evaluation suite
- explicit forbidden action declarations carried into worker context

Control rule:

- agents can request privileged maintenance actions; operator/system policy decides execution
- no privileged maintenance action is auto-executed in Phase 3

Implementation sequence:

0. [x] M20.0: Baseline Eval Harness
1. [x] M20.1 Runtime Operating Contract
2. [x] M20.2 ExecutionPlan Spine
3. [x] M20.3 Decision Inbox v2
4. [x] M20.4 Worker Supervisor v1
5. [x] M20.5 Repo Validation Profiles
6. [x] M20.6 Draft PR + CI Repair Loop
7. [x] M20.7 Expanded Internal Eval Suite

### M20.1 Runtime Operating Contract

Goal:

- make the runtime state visible to both workers and operators

Scope:

- add a versioned `RuntimeManifest`
- expose current capabilities through `GET /system/runtime-manifest`
- pass a frozen run-scoped manifest through `WorkerRequest.runtime_manifest`
- persist the run-scoped manifest as worker-run evidence
- include service identity, build/environment, sandbox image/root, selected worker/profile/runtime,
  workspace id, task budget, delivery mode, network/read-only state, tool permissions,
  forbidden actions, and approval capabilities
- add request-only `MaintenanceRequest` support for `restart_worker`, `recycle_sandbox`,
  `reload_config`, `dependency_refresh`, and operator attention
- show the current runtime manifest on the System dashboard page

Boundaries:

- the system endpoint shows current capabilities
- the worker request and persisted run evidence show what a specific run saw
- maintenance requests do not grant permission to execute privileged actions

Acceptance criteria:

- workers receive the manifest in prompt/context
- operators can inspect the live manifest from API and dashboard
- completed runs retain the frozen manifest for debugging
- tests cover manifest building, maintenance request validation, worker prompt rendering,
  API exposure, and dashboard rendering

### M20.2 ExecutionPlan Spine

Goal:

- make complex task progress observable and resumable without replacing task-level scheduling

Scope:

- add durable `ExecutionPlan` and `ExecutionPlanNode` persistence
- generate plan nodes from `TaskPlan` / `TaskSpec` for complex tasks
- expose optional `execution_plan` on `TaskSnapshot`
- show current node and plan progress in task detail

Node fields:

- `node_id`
- `parent_node_id` or `depends_on`
- `status`: pending, active, blocked, completed, failed, skipped
- `goal`
- `acceptance_criteria`
- `assigned_worker_profile`
- `budget`
- `validation_commands`
- `artifacts`
- `blocker_interaction_id`
- `retry_count`
- `started_at` and `finished_at`

Boundaries:

- existing task-level dispatch remains authoritative
- plan nodes are not individually scheduled in this slice
- dependency fields prepare for future DAG scheduling without enabling fan-out yet
- `blocked` is a first-class node status so decision handling can attach to the right work item

Acceptance criteria:

- complex tasks create a visible plan spine
- task execution can update current-node status
- blocked tasks identify the blocking node and interaction
- dashboard shows plan progress without changing queue semantics

### M20.3 Decision Inbox v2

Goal:

- turn human input from ad hoc task prompts into reusable, deduplicated decision cards

Scope:

- upgrade `HumanInteraction` payloads into decision cards
- dashboard inbox becomes interaction-card based instead of task-count based
- Telegram/dashboard responses store selected option, optional freeform answer, and optional remember preference
- support human-in-the-loop modes: `require_approval`, `proceed_with_flag`, and `notify_only`
- resolved decisions are reused on retry unless the stable decision payload materially changes

Decision payload fields:

- `decision_key`
- `prompt`
- `recommended_option_id`
- `options`
- `allow_freeform`
- `risk_level`
- `scope`
- affected files or approval categories when relevant

Safety rules:

- high-risk decisions use `require_approval`
- medium-risk decisions may use `proceed_with_flag` only when repo/task policy allows it
- low-risk decisions may use `notify_only` when policy and prior outcomes support it
- remember is allowed only for low/medium-risk categories at first
- high-risk decisions such as secrets, auth, deployment, destructive migrations, and broad dependency changes remain one-off or require explicit repo policy
- decision hashing ignores volatile wording, timestamps, task ids, and generated summaries

Acceptance criteria:

- decisions render as cards in dashboard and Telegram-compatible responses
- selected options resume the correct task/node
- retries do not re-ask materially identical resolved decisions
- HITL mode selection is visible in interaction payloads and timeline events
- tests cover decision-card hashing, response persistence, and retry reuse

### M20.4 Worker Supervisor v1

Goal:

- make worker availability, capacity, and unhealthy profiles explicit

Scope:

- add worker registry and heartbeat persistence
- track worker id, process/host identity, capabilities, supported profiles, capacity,
  active task count, last heartbeat, health, and quarantine reason
- make polling respect capacity and lane/profile compatibility
- use capacity-aware claiming for safe routing and backpressure, not multi-agent fan-out
- quarantine provider/auth/sandbox-failing profiles until operator clears them
- add best-effort cancellation propagation where the runtime supports it

Internal slices:

1. registry and heartbeat
2. capacity-aware claiming
3. profile quarantine
4. best-effort cancellation propagation

Cancellation evidence:

- `cancel_requested_at`
- `cancel_signal_sent_at`
- `worker_acknowledged_cancel_at`
- `subprocess_terminated_at`
- `container_recycled_at`

Acceptance criteria:

- workers cannot claim tasks beyond declared capacity
- routing/claiming respects supported profiles
- quarantined profiles stop receiving new work until cleared
- cancellation records best-effort propagation evidence without blocking v1 on perfect worker support

### M20.5 Repo Validation Profiles

Goal:

- reduce manual verification by making repo-specific setup and quality gates explicit

Scope:

- add optional repo-root `code-agent.project.yaml`
- support setup commands, validation tiers, protected paths, approval-required categories,
  and default delivery mode
- make `TaskSpec.verification_commands` inherit repo defaults unless explicitly overridden
- require non-read-only tasks to finish with validation evidence or a bounded failure report

Example profile:

```yaml
setup:
  commands:
    - poetry install

validation:
  quick:
    - poetry run pytest tests/unit
  full:
    - poetry run pytest
    - poetry run pre-commit run --all-files

protected_paths:
  - db/migrations/*
  - .github/workflows/*
  - infra/*

approval_required:
  - dependency_changes
  - auth_changes
  - deployment_changes
  - destructive_migrations

delivery:
  default_mode: draft_pr
```

Acceptance criteria:

- repo profiles parse with clear validation errors
- task specs inherit profile defaults deterministically
- risk/budget can select quick vs full validation
- protected-path and approval-required categories trigger human interaction
- mutable tasks cannot report success without validation evidence or bounded failure evidence

### M20.6 Draft PR + CI Repair Loop

Goal:

- make non-trivial code changes reviewable by default and repair CI failures without manual log spelunking

Scope:

- make `draft_pr` the recommended delivery mode for non-trivial code changes
- persist delivery metadata as first-class task/run evidence
- ingest GitHub CI check failures
- create focused repair tasks from failed checks

V1 boundaries:

- GitHub only
- draft PR only
- CI check ingestion only
- no review-comment repair yet
- no auto-merge
- no deployment

Delivery metadata:

- `delivery_mode`
- `branch_name`
- `pr_url`
- `pr_number`
- `commit_sha`
- `ci_status`
- `ci_failed_jobs`
- `ci_last_checked_at`

Repair task context:

- failing job
- failing command
- log excerpt
- suspected files
- original task id
- original PR

Acceptance criteria:

- draft PR metadata is visible on task snapshots/dashboard
- failed CI creates a focused repair task with enough context to act
- repair tasks link back to the original task and PR
- review-comment ingestion remains explicitly deferred

### M20.7 Expanded Internal Eval Suite

Goal:

- use real tasks as the guardrail for future autonomy changes

Scope:

- expand the M20.0 baseline to 20 to 50 tasks
- classify tasks by babysitting risk
- track success rate, validation pass rate, repeated questions, human interruptions,
  repair loops, worker failures, time to PR, and CI/review rejection rate
- include worker-profile success rates, orchestrator-stage latency, validation-failure categories,
  provider failure causes, and manual-log-inspection rate

Task classes:

- no decision needed, should finish alone
- one clarification needed, then should finish
- permission needed, then should finish
- validation fails, repair should happen
- worker/provider failure, reroute or quarantine should happen
- protected path touched, approval should be requested
- PR CI fails, repair task should be created

Acceptance criteria:

- eval results compare against M20.0 baseline
- reliability regressions are visible before merging future autonomy changes
- metrics emphasize reduced babysitting, not only raw completion rate
- profile/stage metrics are concrete enough to guide future routing and autonomy policy

## Milestone 21: Worker Runtime Hotspot Refactor

Goal:

- reduce maintenance risk in runtime hotspots via incremental internal boundary extraction

Planned internal splits:

- [x] worker facade
- [x] runtime executor
- [x] sandbox/session adapter
- [x] prompt assembler
- [x] tool execution and permission gate
- [x] post-run pipeline
- [x] result mapper

Approach:

- incremental extraction
- preserve existing contracts and behavior
- preserve M20 runtime, plan, decision, supervisor, validation, and delivery contracts
- prioritize testability and reviewability
- avoid behavior-changing refactors until reliability milestones define the right boundaries

## Phase 4: Selective Autonomy After Reliability

Goal:

- expand autonomy only where Phase 3 metrics show the system is reliable enough to earn it

Operating rule:

- Phase 4 changes must be justified by M20/M21 evidence, not competitor parity alone
- personal-use boundaries remain in force
- high-risk actions still require explicit approval

### M22 Eval-Driven Routing

Goal:

- route tasks by observed worker/profile outcomes instead of static preference alone

Scope:

- use M20.0/M20.7 eval results and production run metrics
- compare success, validation pass rate, latency, interruption rate, and failure causes by profile
- update routing policy with explainable profile choices

Boundary:

- routing changes stay policy-driven and inspectable; no opaque auto-training loop

### M23 Memory Admission And Retrieval

Goal:

- make durable memory useful, reviewable, and measurable before adding heavier retrieval infrastructure
- improve memory relevance when evals show keyword/recency retrieval is causing misses

Progress:

- [x] Slice 1: load skeptical personal/project/session memory from the DB before worker dispatch and persist typed worker-produced memory after runs
- [x] Slice 2: add full-text memory search and retrieval visibility before evaluating semantic/vector retrieval
- [x] Slice 3/4: add deterministic retrieval evaluation, curated reviewable memory corpus, memory proposals, dashboard review flow, and SQLite-vs-Postgres FTS evidence
- [x] Slice 5: unify worker memory candidates and reviewable proposals behind a `MemoryAdmissionService`, including a required LangMem and Mem0/OpenMemory adoption spike; see [`docs/m23-slice-5-memory-admission.md`](m23-slice-5-memory-admission.md)
- [x] Slice 6: add an episodic observation layer for raw task/session observations, compact search, timeline/full-observation fetch, recent-session context, private-tag stripping, and an observation-to-admission bridge; see [`docs/m23-slice-6-episodic-observation-layer.md`](m23-slice-6-episodic-observation-layer.md)
- [x] Slice 7: expose observation/admission visibility and lineage through the Knowledge Base UI; see [`docs/m23-slice-7-observation-admission-visibility.md`](m23-slice-7-observation-admission-visibility.md)
- [x] M23.8: deterministic extraction hardening + evaluation for verification commands, pitfalls, remembered instructions, and conventions
- [x] M23.9: read-side memory gate for staleness, conflict, risk, advisory strength, and project-over-personal precedence
- [x] M23.10: shape a deterministic advisory repository memory profile from read-gated project memory without changing executable repository policy (done in pr 308)
- [x] M23.11: evaluation/reliability of actual agent behavior: does the worker use the profile correctly, does it avoid stale policy, and does it improve task success without increasing unsafe actions.

Scope:

- treat `WorkerResult.memory_to_persist` as candidate memory rather than a direct durable-write command
- use risk/decision classification to reject, create, update, merge, or route candidates to human review
- use `memory_proposals` only for candidates that require human approval
- keep durable personal/project memory in the existing Postgres store
- store task/session observations separately from durable memory
- bridge useful observations into `MemoryCandidate` objects that still pass through `MemoryAdmissionService`
- add semantic retrieval behind the existing skeptical-memory contract
- preserve source, confidence, scope, verification, and editability metadata
- consider pgvector only if metrics justify the new infrastructure dependency

Boundary:

- do not add vector storage just because it is available; add it only when measured retrieval quality needs it
- do not let workers write durable memory directly; all candidates pass through admission
- do not add LangMem, Mem0/OpenMemory, Graphiti, Cognee, or another memory platform as a production dependency until the Slice 5 adoption spike proves a clear net simplification
- copy useful memory-system ideas into local, inspectable Postgres-backed components before adopting a full external memory platform
- do not promote raw observations into durable memory without admission

### M24 Decomposed Task DAG

Goal:

- turn the observable ExecutionPlan spine into real subtask decomposition

Scope:

- add a decomposition step that emits sub-TaskSpecs and dependency edges
- keep execution mostly sequential at first
- aggregate node outcomes into a single task result and validation story

Boundary:

- DAG scheduling is introduced after the plan spine, decision model, supervisor, and validation gates are stable

Progress:

- [x] M24.1: typed node contracts, deterministic dependency validation, six-node limit, and safe monolithic fallback
- [x] M24.2: persisted node TaskSpecs, worker evidence, outcome fields, API snapshots, and dashboard visibility
- [x] M24.3: sequential topological node execution in one parent task/workspace through the existing worker runtime
- [x] M24.4: deterministic node outcome aggregation into the parent result and validation story
- [x] M24.5: node-specific approval/blocking integration, bounded retry, and resume from completed nodes
- [x] M24.6: reliability evaluation gate before M25 fan-out
  - [x] run a deterministic sequential DAG scenario matrix and publish its CI report
  - [x] persist per-node execution timing, runtime/trace identity, and sanitized effective input
    for operator inspection
  - [x] preserve persisted generated-node contracts across retry and resume
  - [x] add deterministic branching-and-join coverage without concurrent execution

### M24.9.5 Temporal Runtime Consolidation — Complete

Goal:

- consolidate the Temporal runtime boundary proven by the M24.9 PoC before
  introducing concurrent DAG execution

Scope:

- make Temporal the guarded default for new sequential tasks while retaining an
  explicit legacy fallback during drain
- standardize retry, timeout, heartbeat, cancellation, and terminal-failure
  projection semantics for Temporal activities
- make task, interaction, and timeline projections idempotent and safe for
  operator/workflow multi-writer updates
- inventory LangGraph durable-control-flow responsibilities and define the
  brain/node-runner extraction boundary
- classify legacy queue, lease, heartbeat, and worker-registry code as custom
  policy, fallback-only, or a future deletion candidate

Boundary:

- no parallel fan-out, worktree mutation, queue deletion, or broad LangGraph
  rewrite in this milestone

### M25 Parallel Worker Fan-Out

Goal:

- run independent plan nodes concurrently where it clearly reduces completion time without reducing reliability

Scope:

- fan out only dependency-independent nodes
- respect worker capacity, lane quotas, sandbox limits, and repo validation gates
- add aggregation and reviewer checkpoints before final delivery

Boundary:

- no broad multi-agent swarm behavior; parallelism is selective, bounded, and measured

Progress:

- [x] M25.0: explicit DAG dependency and parallel-safety semantics
  - preserve omitted versus explicit-empty dependencies, persist node metadata,
    validate the read-only fan-out contract fail-closed, and retain legacy
    sequential compatibility without enabling concurrent execution
- [ ] M25.1: durable node activity contract
  - add typed node results, logical-attempt idempotency, and independent
    node persistence without concurrent writes to parent Temporal state

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
3. Milestone 24
4. Milestone 25
5. Milestone 26
6. Milestone 27

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

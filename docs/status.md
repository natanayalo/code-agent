# Status

## Current phase

Phase 4A: Temporal stabilization, execution architecture foundation, and measured reliability.

Active focus: **M31 — Proactive Operations and Safe Scheduled Work**
entry prerequisites (supported Temporal compatibility baseline and schedule execution foundation).
M30 is completed with authenticated GitHub event intake, issue/delivery timeline mapping,
draft PR synchronization, issue task replay (`/code-agent replay`), and Temporal
interaction primitives (Queries & Updates).
M29 is completed with all four canonical task-class/mode cells empirically
qualified; production routing remains static and M27 deferred.

## Current capabilities

- authenticated API, generic webhook, Telegram, and GitHub-native webhook task intake
- durable Postgres persistence for users, sessions, tasks, worker runs,
  interactions, timelines, artifacts, memory, and delivery metadata
- transactional Temporal command outbox with idempotent start, signal, and
  cancellation delivery
- Temporal-owned task lifecycle, retry, timeout, signal wait, cancellation,
  and activity recovery
- TaskSpec generation, capability-aware worker routing, and manual overrides
- Codex and Antigravity native-agent workers plus opt-in OpenRouter execution
- isolated Docker workspaces with command, diff, test, and artifact evidence
- skeptical personal/project memory, compact session state, admission review,
  observation evidence, and full-text retrieval
- clarification, approval, permission escalation, cancellation, and replay
  controls through API, dashboard, and GitHub issue comments (`/code-agent replay`)
- Temporal-native interaction primitives: read-only coordination inspection via
  `@workflow.query` (`get_status`, `get_current_plan`, `get_pending_decision`)
  and synchronous phase-validated operator decision application via
  `@workflow.update` (`apply_operator_decision`) with command-key deduplication
- sequential decomposed-task DAG execution and opt-in bounded two-node
  read-only fan-out
- deterministic and independent verification, independent review, and
  workspace/branch/draft-PR delivery
- durable, bounded verifier and independent-review repair loops on retained
  workspaces with permission escalation, re-verification, and manual handoff
- automated review-comment polling and repair loop on open draft PRs with
  author filtering, budget capping, thread replies, and dashboard visibility
- dashboard visibility for tasks, TaskSpec, DAG attempts, interactions,
  timelines, logs, artifacts, traces, memory, proposals, metrics, tools,
  dependency readiness, degraded reasons, and safe recovery guidance
- public dependency-aware `/ready` and authenticated outbox, worker freshness,
  stuck-wait, and terminal-reconciliation metrics
- an incremental M25.6 evidence collector that pins deployment identity,
  captures Postgres and Temporal proof, rejects duplicate cases, and emits a
  sanitized aggregate without changing routing automatically
- an offline read-only M29 provider reliability advisory report CLI and versioned
  contract aggregating real-task outcomes by task class, profile, and mutation
  mode with 95% Wilson confidence intervals, fallback recommendations, and
  public allowlist redaction
- an offline read-only M29 provider reliability robustness analysis CLI and
  versioned contract evaluating 30/60/90-day window variation, non-overlapping
  historical/recent temporal cohorts, and deterministic bootstrap resampling with
  public allowlist redaction
- an M29 feature/mutation evaluation runner, deterministic taskboard fixture
  builder (`evaluation/m29_mutation_fixture.py`), runner-owned read-only acceptance
  oracles (`evaluation/m29_mutation_oracles.py`), concrete external evaluator
  with blinded neutral review (`evaluation/m29_mutation_verifier.py`,
  `evaluation/m29_mutation_runner.py`) standardized on local Ollama
  `qwen / qwen2.5-coder:7b` as the zero-cost neutral evaluator (`DEFAULT_M29_NEUTRAL_EVALUATOR`,
  suite SHA-256 `81ac140df0bbbd18cd243abde2d7b86fe74b250cf0ac02e2ec5ffc208e69e987`),
  and published 20-case live `feature/mutation` wave artifacts
  (`evaluation/m29_provider_reliability_mutation_manifest.json`,
  `evaluation/m29_provider_reliability_mutation_paired_analysis.{json,md}`)
  qualifying `feature/mutation` (Codex $N=31$, Antigravity $N=26$) with advisory
  recommendation `codex-native-executor`; production routing remains static

- synchronized pull-request and CI check-run state projected into dashboard
  task summaries and operator inbox with direct links, issue-to-PR lineage, and
  explicit safe next actions (M30 slice 1a)

Completed work remains in [`CHANGELOG.md`](../CHANGELOG.md). The historical
Temporal migration and rollback record is in the
[cutover archive](archive/temporal_cutover.md).

## Current evidence

- The accepted M25.3 release covered the Temporal lifecycle, HITL, cancellation,
  restart recovery, sequential DAGs, opt-in fan-out, outage recovery, history
  replay, and terminal reconciliation.
- The accepted M25.4 release added patch-aware Temporal completion-loop parity:
  verifier and independent-review repair, retained-workspace execution, repair
  restart/idempotency/cancellation handling, replay coverage, and one terminal
  manual-follow-up projection when repair cannot complete.
- The accepted M25.5 slice proves structured dependency failure and same-process
  recovery, worker-owned dispatcher heartbeats, stale outbox gating,
  task-specific degraded signals, dashboard recovery guidance, responsive
  operator rendering, and a current-branch real-worker Temporal lifecycle.
- The reviewed M25.6 real-worker baseline contains 20/20 valid captures across
  monolithic read-only work, mutations, verifier and independent-review repair,
  sequential DAGs, read-only fan-out, HITL, cancellation, worker restart, and
  isolated Codex/Antigravity draft-PR delivery. Operator redaction review and
  the forbidden-field validator passed for the public reports. The result does
  not change routing automatically.
- The completed M26 slice adds automated review-comment polling on open draft
  PRs, structured prompt building for review comments, deduplicated reply
  planning, immediate per-reply DB checkpointing, and GitHub GraphQL thread replies.
- The M28.2 slice records typed task, route, approval, worker, verifier, and
  review outcomes in compact session state, then makes that bounded advisory
  context available to replayed and resumed workers.
- The reviewed M28 real-worker matrix contains 8/8 valid Codex/Antigravity
  pairs on the exact evaluated revision. It proves accepted project memory
  reaches native prompts and is used where expected; irrelevant, stale, and
  conflicting memory is rejected or suppressed as required. Questions and
  interventions did not regress (all captures were zero); mixed completion
  times are observations, not a timing-improvement claim.
- The 25-case frozen evaluation remains a deterministic domain-logic regression
  suite. It uses replayed worker outcomes and is not a real-provider Temporal
  reliability baseline.
- Performance routing currently consumes checked-in advisory metrics; it is not
  refreshed from persisted live task outcomes.
- Full-text memory retrieval passes the curated non-semantic regression cases
  and retains known synonym gaps. Current evidence does not justify adding a
  vector dependency.

- The completed M28.5A slice defines the 7 trust boundaries, STRIDE threat
  model, sandbox capability contracts (`SandboxCapabilityGrant`), broker-owned
  secret definitions (`RegisteredSecretDefinition`), audience intersection rules,
  and ingress secret stripping adapters (`LegacyIngressTaskRequest`,
  `IngressMigrationAdapter`).
- The completed M28.5C slice implements the provider-neutral `AgentEvent`
  foundation with 11 canonical event variants, `CodexStreamNormalizer`,
  `AntigravityStreamNormalizer`, stream capping, secret redaction, reasoning
  suppression, opt-in `CODE_AGENT_NATIVE_EVENT_CAPTURE_ENABLED` flag wiring,
  and `agent-events-v1.jsonl` artifact capture while preserving `WorkerResult`
  contract compatibility and zero DB migration requirements.

- The M29 provider reliability advisory report extracts real-task
  Temporal/native-agent evidence from PostgreSQL, enforces a 90-day window and
  10-task minimum per cell, computes 95% Wilson score confidence intervals,
  requires >= 2 eligible compatible candidates for recommendations, and validates
  public-field sanitization without modifying live routing. Following the discovery of
  OpenAI's `gpt-5.4-mini` retirement, the evaluation architecture was reshaped to
  enforce authoritative, model-aware execution cohorts (`provider`, `model`,
  `reasoning_effort`) before aggregation. Wave 1 remains an immutable diagnostic
  baseline, Wave 2 established `docs/read_only`, and Wave 3
  (`artifacts/m29_evidence_bundle_wave3/bundle.json`, ignored/private) completed
  40 frozen read-only cases (20 investigation and 20 feature; 10 per
  provider/cell) at build `a32719d114fab93c8e63e81153229ac900748a5c`. All 40
  cases reached immutable terminal outcomes (18 completed, 22 failed), with
  zero changed files, zero unresolved interactions/gates, and Temporal/native
  execution throughout. The fixed Wave 3 terminal timestamp is
  `2026-09-19T21:32:22.562107Z`.

  The Wave 3 canonical, operational, and robustness snapshot used that timestamp
  and a 10,000-iteration bootstrap with seed 29. That historical canonical report qualifies
  `feature/read_only` at $N=10$ vs $10$ (5/10 accepted for each provider) and
  recommends `antigravity-native-executor-read-only` on lower successful-task
  latency (202.93s versus 359.37s; terminal latency remains operational);
  `investigation/read_only` remains insufficient-data because authoritative
  `model_execution` identity is available for only $N=9$ Codex and $N=6$
  Antigravity cases, so the fail-closed extractor does not infer or backfill
  missing samples. The feature recommendation is a terminal-completion/latency
  advisory rather than an independently verified quality claim because persisted
  verification warnings yield a strict 0% verification-pass metric. The committed
  sanitized case manifest and paired supplement bind outcomes to the suite/build
  without exposing the private bundle. `feature/mutation` remains pending,
  robustness is `partial` with no historical 45-day split, and M27 resumption
  remains deferred. Production routing is static and unchanged.

- Wave 4 closes the cumulative investigation/read-only sample-floor gap. The
  final ignored/private bundle
  `artifacts/m29_evidence_bundle_wave4_from_first_quota/bundle.json` contains 20
  terminal outcomes (16 completed, 4 failed: 3 timeouts, 1 unknown), all
  Temporal/native investigation cases with zero changed files, interactions,
  or gate failures. Persisted identity verifies all 10 Antigravity cases
  (`gemini-3.8-flash/medium`) and 7 Codex cases (`gpt-5.6-luna/high`); 3 Codex
  cases are excluded as `unknown_execution_identity`, with no inferred identity.
  The sanitized manifest also records one earlier verified Codex
  `infra_verifier_unavailable` attempt for
  `m29-w4-inv-02-repair-loop-codex`; it is included in the cumulative cohort,
  not the final-bundle paired analysis.
- At `2026-09-27T22:02:06.727893Z`, the refreshed cumulative canonical cohort
  qualifies `investigation/read_only` at N=16 Antigravity (15 accepted) and N=17
  Codex (9 accepted). Antigravity is recommended on the primary Wilson lower
  bound ($0.7167$ vs $0.3096$); successful-task medians (504.25s vs 1012.65s)
  are supporting evidence, not a tie-break. Exact accounting is Wave 3 baseline
  + eligible final Wave 4 cases + the documented prior attempt: `6+10=16`
  Antigravity and `9+7+1=17` Codex.
- Across all 10 final-bundle pairs, 6 both completed and 4 were Antigravity-only.
  Among 7 identity-complete pairs, 6 both completed and 1 was Antigravity-only
  (0 Codex-only, 0 neither). The paired bootstrap is 10,000 iterations/seed 29.
  Robustness ranks Antigravity
  first in 30/60/90-day windows and has a 99.86% unpaired first-rank
  probability, but remains `partial`: the historical 45-day cohort has zero
  included tasks. `feature/mutation` remains N=0; routing and
  `evaluation/routing_metrics.json` are unchanged.
- The [`feature/mutation` evaluation contract](../evaluation/m29_feature_mutation_contract.md)
  is completed and published. The 20-case live `feature/mutation` wave evaluated
  10 Codex and 10 Antigravity tasks on the frozen disposable taskboard fixture
  revision `228071722c645e813af16053e56d007c25e9f127`. All 10 Codex tasks
  completed (10/10 accepted); 9 Antigravity tasks completed (9/10 accepted; 1
  terminal failure `tool_runtime` on `m29-mut-07-retry-delay-antigravity`). Runner-owned
  read-only acceptance oracles and neutral review by local Ollama `qwen / qwen2.5-coder:7b`
  confirmed post-terminal independent quality: 10/10 quality pass for Codex and
  8/10 for Antigravity (1 task failure on `m29-mut-07-retry-delay-antigravity`, 1
  acceptance oracle failure on `m29-mut-04-cursor-page-antigravity`).
  Reconciled cumulative 90-day cohort accounting gives Codex $N=31$ (30 accepted,
  Wilson lower bound $0.8381$, median successful latency $25.5\text{s}$) and
  Antigravity $N=26$ (23 accepted, Wilson lower bound $0.7102$, median successful
  latency $178.7\text{s}$). The advisory recommendation recommends `codex-native-executor`
  for `feature/mutation` (92.11% bootstrap win probability). All four canonical
  M29 cells (`docs/read_only`, `feature/read_only`, `investigation/read_only`,
  and `feature/mutation`) are now empirically qualified with $\ge 10$ samples per
  provider. Robustness remains `partial` because model-aware execution identity is
  37 days old (<45-day historical split); M27 remains deferred and production routing
  is static.

## Known limitations

- bounded fan-out remains an explicit read-only pilot and is disabled by
  default
- deep-scout repo-to-research chaining is deferred and is not part of the
  supported Temporal completion loop
- M28's scoped matrix does not establish a general completion-time reduction;
  it is evidence of correct, safe memory behavior across the defined cases
- the trusted Temporal worker retains Docker authority, while native provider
  CLI execution is confined to one-shot task containers; a separate sandbox
  broker remains deferred; live Docker container runtime enforcement of M28.5A
  contracts is now completed (M28.5A.2).
- lifecycle data overlaps across Temporal history/workflow state, serialized
  `TemporalTaskState`, task/product projections, and execution-plan rows;
  intermediate state pruning (Waves 1-3) has eliminated ephemeral notifications,
  candidate metadata, plan models, and node outcomes from snapshots while
  relational projections and markers retain authoritative execution truth
- the worker boundary maintains terminal `WorkerRequest -> WorkerResult` compatibility;
  streaming `AgentEvent` capture is opt-in via `CODE_AGENT_NATIVE_EVENT_CAPTURE_ENABLED`;
  versioned pre-dispatch `ContextEnvelope` context assembly and artifact persistence
  is enabled by default (M28.5D)
- native-agent command audit and several orchestration/worker adapters remain
  complexity hotspots
- the repository enforces a 90% Python coverage target in CI

## Next slices only

1. Prepare **M31 — Proactive Operations and Safe Scheduled Work**:
   a. establish supported Temporal compatibility baseline and schedule execution foundation.
2. Maintain completed M29 advisory report contracts and static production routing:
   a. all four canonical task-class/mode cells are qualified (`docs/read_only`, `feature/read_only`, `investigation/read_only`, `feature/mutation`);
   b. longitudinal 45-day split robustness remains calendar-gated on model-aware task age (expected ~2026-11-02 for Wave 2/3 tasks and ~2026-12-08 for `gpt-6-luna`);
   c. M27 autonomy policy remains deferred.

## Deferred

- M27 reliability-based autonomy remains reserved until real-task metrics can
  support reversible policy thresholds. All four M29 cells now have qualified
  advisory recommendations, but 45-day longitudinal split robustness remains
  calendar-gated on task age.
- durable child workflows, broad mutable fan-out, isolated worktree/patch
  reconciliation, an operator-visible agent tree, and evidence-backed
  procedural skills remain future/conditional ideas rather than current work.

# M29 Provider Reliability Threshold Analysis

This is the refreshed, offline advisory analysis for M29 Wave 3. It uses the
canonical current-execution-cohort report and the operational diagnostic report
generated at the one frozen terminal timestamp `2026-09-19T21:32:22.562107Z`.
Production routing and `evaluation/routing_metrics.json` are unchanged.

## 1. Evidence provenance and integrity

Wave 3 is pinned to:

- suite: `evaluation/m29_live_provider_suite_wave3.json`
- harness build: `a32719d114fab93c8e63e81153229ac900748a5c`
- target repository revision: `f0b4cb8139d1497857d1b67e106a6170cbbed05c`
- suite SHA-256: `5c9fc97979495bbfbe55115c16f22e419925f1079960e90e5a8db2b8b94e99f6`
- ignored/private bundle: `artifacts/m29_evidence_bundle_wave3/bundle.json`

The frozen suite contains 40 adjacent, counterbalanced provider pairs: 20
investigation cases and 20 feature cases, with 10 cases per provider/cell.
Every case reached an immutable terminal outcome (18 completed, 22 failed).
The bundle records 20 Codex and 20 Antigravity cases, all in Temporal/native
execution, with zero changed files, zero unresolved interactions, and zero gate
failures. Completed cases were never rerun; only in-flight cases were resumed.

The failure taxonomy in the bundle is 10 `infra_verifier_unavailable`, 5
`tool_runtime`, 2 `unknown`, and 5 `worker_failure`. These failures remain
evidence and are not silently converted to passes.

## 2. Report accounting

The canonical extractor is deliberately fail-closed: it accepts only tasks with
authoritative `budget_usage["native_agent"]["model_execution"]` identity matching
the pinned provider, model, and reasoning effort. It does not infer identity from
profile names or task text.

| Metric | Canonical current cohort | Operational diagnostic |
|---|---:|---:|
| Tasks scanned | 434 | 434 |
| Included tasks | 55 | 328 |
| Excluded tasks | 379 | 106 |
| `unknown_execution_identity` | 273 | 0 |
| `non_temporal_runtime` | 81 | 81 |
| `malformed_inconsistent_timeline` | 10 | 10 |
| `cancelled` | 9 | 9 |
| `evaluation_smoke` | 4 | 4 |
| `non_native_agent_mode` | 2 | 2 |

The operational view is `diagnostic_only`; it intentionally does not rank
heterogeneous model vintages.

## 3. Canonical cell results

The canonical policy uses a 90-day lookback, a 10-sample floor per compatible
candidate, and 95% Wilson lower bounds.

| Task class | Mode | Codex | Antigravity | Decision |
|---|---|---:|---:|---|
| `docs` | `read_only` | N=10, 10 accepted | N=10, 9 accepted | Recommend Codex |
| `feature` | `mutation` | N=0 | N=0 | Insufficient data |
| `feature` | `read_only` | N=10, 5 accepted, median 354.22s | N=10, 5 accepted, median 76.65s | Recommend Antigravity |
| `investigation` | `read_only` | N=9, 3 accepted | N=6, 5 accepted | Insufficient data |

`feature/read_only` is therefore qualified at the canonical sample floor. Both
providers have Wilson lower bound `0.2366`; Antigravity ranks first on lower
median latency and is the advisory recommendation. The 10,000-iteration,
seed-29 bootstrap assigns Antigravity a `58.61%` win probability.

`investigation/read_only` is not qualified. Five Wave 3 investigation outcomes
do not have authoritative model-execution identity in the persisted task rows,
so the extractor correctly excludes them. The suite's 20 terminal outcomes are
preserved in the private bundle, but missing identity cannot be repaired by
inference or by rerunning terminal cases.

## 4. Robustness and threshold interpretation

The robustness report is `partial`. The current cohort has no included tasks in
the historical 45-day split, so longitudinal consistency and temporal window
sensitivity are not established. The report uses 10,000 bootstrap iterations
with seed 29; its complete cells are `docs/read_only` and `feature/read_only`,
while `feature/mutation` and `investigation/read_only` are
`insufficient_data`.

The sample floor remains a qualification gate, not a routing trigger. A cell
with fewer than 10 authoritative samples per candidate receives no
recommendation even if its observed acceptance rate is high. The same rule
keeps mutation evidence separate from this strictly read-only wave.

## 5. Decision record and remaining work

1. Qualify `docs/read_only` with the existing Codex advisory recommendation.
2. Qualify `feature/read_only` with the Antigravity advisory recommendation.
3. Keep `investigation/read_only` insufficient-data until an identity-complete
   wave supplies 10 samples per provider.
4. Keep `feature/mutation` pending; it requires a separate mutable-evaluation
   contract with explicit delivery and cleanup safeguards.
5. Keep longitudinal consistency pending; the historical split is empty.
6. Keep M27 deferred and production routing static. Any future routing change
   requires a separate reviewed, reversible policy change with held-out
   validation.

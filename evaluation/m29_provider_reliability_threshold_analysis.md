# M29 Provider Reliability Threshold Analysis

This analysis preserves the Wave 3 decision record and appends the refreshed
Wave 4 investigation/read-only result. The current canonical
current-execution-cohort report, operational diagnostic report, and robustness
report use the frozen Wave 4 terminal timestamp
`2026-09-27T22:02:06.727893Z`. Production routing and
`evaluation/routing_metrics.json` are unchanged.

## 1. Wave 3 evidence provenance and integrity (historical snapshot)

Wave 3 is pinned to:

- suite: `evaluation/m29_live_provider_suite_wave3.json`
- harness build: `a32719d114fab93c8e63e81153229ac900748a5c`
- target repository revision: `f0b4cb8139d1497857d1b67e106a6170cbbed05c`
- suite SHA-256: `5c9fc97979495bbfbe55115c16f22e419925f1079960e90e5a8db2b8b94e99f6`
- sanitized case manifest SHA-256: `d49ffb5e278621744f8bc1c6880054df9bce02b2a569df67446d81412cee4b0c`
- advisory report SHA-256: `9ec0c05ae293719489f82a6403325766832f172b7983d647d28d241d382ae9f1`
- operational report SHA-256: `bf77df26c13de971a2aa6caaedd66aa24002b147096d9a34903fe591594949d9`
- robustness report SHA-256: `931be8c1ae3a6b48fa80134f43d6275bf4b4eeddde04b5acab21d9385b5ad383`
- paired supplement SHA-256: `08faffb4d9b28e1dd39810f386ca6e296aff884779a1cff838485f87f6111c4c`
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

## 2. Wave 3 report accounting

The canonical extractor is deliberately fail-closed: it accepts only tasks with
authoritative `budget_usage["native_agent"]["model_execution"]` identity
matching the pinned provider, model, and reasoning effort. It does not infer
identity from profile names or task text.

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

For the Wave 3 cases specifically, all five identity-incomplete investigation
outcomes were failures: one intended Codex case and four intended Antigravity
cases. The public manifest records those terminal failures and their
`unknown_execution_identity` exclusions without inferring a provider. This is
why investigation/read-only remains insufficient-data rather than a provider
quality claim.

## 3. Wave 3 canonical cell results

The canonical policy uses a 90-day lookback, a 10-sample floor per compatible
candidate, and 95% Wilson lower bounds.

| Task class | Mode | Codex | Antigravity | Decision |
|---|---|---:|---:|---|
| `docs` | `read_only` | N=10, 10 accepted | N=10, 9 accepted | Recommend Codex |
| `feature` | `mutation` | N=0 | N=0 | Insufficient data |
| `feature` | `read_only` | N=10, 5 accepted, success median 359.37s | N=10, 5 accepted, success median 202.93s | Recommend Antigravity |
| `investigation` | `read_only` | N=9, 3 accepted | N=6, 5 accepted | Insufficient data |

`feature/read_only` is qualified at the canonical sample floor. Both providers
have Wilson lower bound `0.2366`; Antigravity ranks first on the lower median
successful-task latency and is the advisory recommendation. Its median failure
latency is `25.16s` versus Codex `349.07s`, while median terminal latency is
`76.65s` versus `354.22s`. Reporting all three distributions prevents the
recommendation from rewarding fast failures as if they were successful work.
The 10,000-iteration, seed-29 unpaired bootstrap assigns Antigravity a `58.61%`
first-rank probability under this successful-latency tie-break.

Acceptance is terminal task completion, not an assertion that every optional
stage passed. Persisted production-shaped `verification_completed` events with
status `warning` are applicable but not passed; a completed task carrying that
warning remains accepted. Therefore the feature/read-only cells' verification
pass rate of `0.0` is a strict stage metric and not an independently verified
quality rate. The recommendation is a terminal-completion/latency advisory,
not a claim of independently verified feature quality.

## 4. Wave 3 paired analysis and robustness interpretation

The committed paired supplement (`evaluation/m29_provider_reliability_wave3_paired_analysis.{json,md}`)
resamples complete topic pairs with seed 29 and 10,000 iterations. It is a
descriptive supplement to the existing unpaired bootstrap, not a forecast or
routing threshold.

| Task class | Pairs | Both completed | Codex only | Antigravity only | Neither | Identity-complete pairs | Successful AG-Codex median delta |
|---|---:|---:|---:|---:|---:|---:|---:|
| `feature` | 10 | 5 | 0 | 0 | 5 | 10 | `-150.79s` |
| `investigation` | 10 | 3 | 0 | 2 | 5 | 5 | `-552.85s` |

The feature pair outcomes are symmetric on acceptance (five both completed and
five neither), while Antigravity is faster in all five identity-complete
successful pairs. Investigation pairing remains descriptive only because five
pairs are not identity-complete and the cell is below the sample floor.

The robustness report is `partial`. The current cohort has no included tasks in
the historical 45-day split, so longitudinal consistency and temporal window
sensitivity are not established. `feature/mutation` and
`investigation/read_only` remain `insufficient_data` in the robustness report.

## 5. Wave 3 decision record (historical)

1. Qualify `docs/read_only` with the existing Codex advisory recommendation.
2. Qualify `feature/read_only` with the Antigravity terminal-completion/latency
   advisory, with the verification-stage limitation stated above.
3. Keep `investigation/read_only` insufficient-data until an identity-complete
   wave supplies 10 samples per provider.
4. Persist configured and actual execution identity before dispatch in a future
   worker/runtime follow-up so early failures are attributable without
   historical backfill. This PR intentionally does not change the production
   worker/database contract.
5. Keep `feature/mutation` pending; it requires a separate mutable-evaluation
   contract with explicit delivery and cleanup safeguards.
6. Keep longitudinal consistency pending; the historical split is empty.
7. Keep M27 deferred and production routing static. Any future routing change
   requires a separate reviewed, reversible policy change with held-out
   validation.

## 6. Wave 4 investigation/read-only result (current snapshot)

Wave 4 used the frozen 20-case suite at build and target revision
`5f0cfdc0ed650dcfdfaff88aa1a23dc2abb8959f` with suite SHA-256
`516ae550a441d5f9eb75d7022c6698f8248d9f0f1ec99ef2d80ef02fce63e3c3`.
The private, ignored retry bundle is
`artifacts/m29_evidence_bundle_wave4_from_first_quota/bundle.json`; the earlier
Wave 4 bundle is preserved separately. Collection resumed at the first
quota-limited case, retaining earlier terminal outcomes and never replacing a
terminal result. All 20 cases reached immutable terminal outcomes: 16 completed
and 4 failed (3 timeouts and 1 unknown failure). All used Temporal/native
investigation/read-only execution; changed-file count, unresolved interactions,
and gate failures were zero.

Persisted execution identity is complete and matches the frozen identity for all
10 Antigravity cases (`gemini-3.8-flash/medium`) and 7 Codex cases
(`gpt-5.6-luna/high`). Three Codex outcomes have unknown legacy execution
identity and are excluded fail-closed; no identity is inferred from the profile
or prompt. The sanitized manifest reconciles all eligible Wave 4 tasks against
the canonical PostgreSQL extractor. Its SHA-256 is
`5791b0506675416e1e1cbd760f0dbef46b8544e6b71b1c63ad7b5a72835d0438`; the
paired-analysis JSON SHA-256 is
`115a155901741ea1fb2523b1bde7550d1d95838203375ad022294f79d6e75c83`.

At the frozen `as_of`, the cumulative 90-day current-execution cohort qualifies
both providers at the 10-sample floor for `investigation/read_only`:

| Profile | N | Accepted | Wilson lower bound | Successful-task median | Terminal median |
|---|---:|---:|---:|---:|---:|
| `antigravity-native-executor-read-only` | 16 | 15 | 0.7167 | 504.25s | 487.41s |
| `codex-native-executor-read-only` | 17 | 9 | 0.3096 | 1012.65s | 902.79s |

The advisory recommends `antigravity-native-executor-read-only`, ranked first on
the canonical successful-task latency tie-break after acceptance eligibility.
This is a cumulative cohort result, not a Wave 4-only sample: the Wave 4 bundle
contributes 10 completed Antigravity cases and 6 completed Codex cases among 7
identity-verified Codex cases. Failure and terminal latency remain reported
operationally and are not used to reward fast failures.

The Wave 4 paired supplement has 10 topic pairs but only 7 identity-complete
pairs: 6 pairs both completed and 4 had only Antigravity complete (0 Codex-only,
0 neither). Across 10,000 paired bootstrap iterations with seed 29, Antigravity
has a 66% higher-acceptance probability, 34% tied probability, and is faster in
successful latency in all bootstrap resamples; the median paired successful
latency delta (Antigravity minus Codex) is -480.76s across 6 successful pairs.
These clustered paired estimates are descriptive and do not replace the
canonical independent-cell report.

Robustness ranks Antigravity first for this cell in all 30/60/90-day windows;
the 10,000-resample unpaired first-rank probability is 99.86% (seed 29). The
robustness report remains `partial`: the historical 45-day cohort contains 0
included tasks, so longitudinal stability is untested. `feature/mutation`
remains at N=0 and has no recommendation. The provider recommendation is
advisory only; M27 remains deferred and neither live routing nor
`evaluation/routing_metrics.json` was changed.

## 7. Updated decision record and remaining M29 work

1. Retain the existing advisory recommendations for `docs/read_only` and
   `feature/read_only`; add Antigravity as the `investigation/read_only`
   advisory recommendation based on the reconciled cumulative cohort above.
2. Keep exact identity persistence fail-closed. The three Wave 4 Codex
   `unknown_execution_identity` exclusions remain evidence gaps; improving
   pre-dispatch identity persistence is a separate worker/runtime follow-up and
   was not included here.
3. Keep `feature/mutation` pending at N=0 until its separate mutable-evaluation
   contract, delivery safeguards, and cleanup plan are reviewed.
4. Gather evidence in a historical 45-day cohort before claiming longitudinal
   robustness; the current historical split has zero included tasks.
5. Evaluate hierarchical budget controls separately. Keep M27 deferred and
   production routing static pending separate review, reversible policy work,
   and held-out validation.

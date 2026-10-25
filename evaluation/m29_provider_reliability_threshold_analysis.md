# M29 Provider Reliability Threshold Analysis

This analysis preserves the historical Wave 3 and Wave 4 decision records and
appends the reconciled Wave 5 feature/mutation result. The canonical
current-execution-cohort report, operational diagnostic report, and robustness
report use the frozen Wave 5 terminal timestamp
`2026-10-26T09:12:04.572097Z`. Production routing and
`evaluation/routing_metrics.json` remain unchanged.

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
`5a8fb26a26c30e837f76b674966f2e3f27ac204a5e6b3e531fb77c1e05612bfb`; the
paired-analysis JSON SHA-256 is
`8bb6e5fa8686c4427a298709d323988bfdc0047f6ffae2b8e21bc4b58c30fe7d`.

At the frozen `as_of`, the cumulative 90-day current-execution cohort qualifies
both providers at the 10-sample floor for `investigation/read_only`:

| Profile | N | Accepted | Wilson lower bound | Successful-task median | Terminal median |
|---|---:|---:|---:|---:|---:|
| `antigravity-native-executor-read-only` | 16 | 15 | 0.7167 | 504.25s | 487.41s |
| `codex-native-executor-read-only` | 17 | 9 | 0.3096 | 1012.65s | 902.79s |

The advisory recommends `antigravity-native-executor-read-only` on the primary
reliability criterion: its Wilson lower bound (`0.7167`) exceeds Codex
(`0.3096`). Successful-task latency (504.25s versus 1012.65s) is supporting
evidence, not a tie-break. This is a cumulative cohort result, not a Wave 4-only
sample. The 20-case final Wave 4 bundle contributes 10 eligible Antigravity and
7 eligible Codex cases; the cumulative Codex cell also includes one earlier,
identity-verified `infra_verifier_unavailable` attempt for
`m29-w4-inv-02-repair-loop-codex`. The sanitized manifest records that attempt
as supplemental cumulative evidence and excludes it from the paired analysis.
Thus the exact cell accounting is Wave 3 baseline + final Wave 4 cases + the
documented prior attempt: Antigravity `6 + 10 = 16`; Codex `9 + 7 + 1 = 17`.
Failure and terminal latency remain reported operationally and are not used to
reward fast failures.

The Wave 4 paired supplement reports outcomes across all 10 final-bundle topic
pairs: 6 pairs both completed and 4 had only Antigravity complete. Of the 7
identity-complete pairs used by the paired bootstrap, 6 had both providers
complete and 1 was Antigravity-only (0 Codex-only, 0 neither). The supplemental
earlier Codex attempt is not part of these case-pair counts. Across 10,000
paired bootstrap iterations with seed 29, Antigravity has a 66% higher-
acceptance probability and 34% tied probability; the median paired successful
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

## 7. Wave 4 decision record (historical snapshot)

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

## 8. Wave 5 feature/mutation evidence and cohort reconciliation

Wave 5 used the frozen 20-case paired `feature/mutation` suite
`evaluation/m29_live_provider_suite_mutation.json` at build SHA
`036bde95c74d517f80c17e6bf32e92d300e3b873` and fixture revision
`228071722c645e813af16053e56d007c25e9f127`, with suite SHA-256
`81ac140df0bbbd18cd243abde2d7b86fe74b250cf0ac02e2ec5ffc208e69e987`.
Neutral evaluation was performed by local Ollama `qwen / qwen2.5-coder:7b`
(`DEFAULT_M29_NEUTRAL_EVALUATOR`) with runner-owned read-only acceptance and
regression oracles.

All 20 cases reached immutable terminal outcomes (19 completed, 1 failed):
all 10 Codex cases (`codex-native-executor`) completed successfully; 9 of 10
Antigravity cases (`antigravity-native-executor`) completed successfully, with 1
terminal failure (`tool_runtime` on case `m29-mut-06-api-ratelimit-ag`).

Persisted execution identity matches the frozen candidate identities for all
20 cases: Codex `gpt-6-luna/high` and Antigravity `gemini-3.8-flash/medium`.
The sanitized manifest reconciles all eligible Wave 5 tasks against the canonical
PostgreSQL extractor at frozen cutoff `2026-10-26T09:12:04.572097Z`. Its SHA-256
is `ac863335239078210cd58f792b966b0a81c0b7f2d5bad387e4d48d21fdcece87`; the
paired-analysis JSON SHA-256 is
`dd144d6d67b3c50984a383d8decf5c7073ced0fc97ae7dc579d3fdf229d2f2d1`.

### 8.1 Cumulative cohort accounting

The canonical 90-day current-execution cohort reconciles the pre-existing baseline
and the Wave 5 suite:

- **Codex (`codex-native-executor`)**:
  - Baseline: 21 tasks (20 accepted, 1 failure: `worker_failure`)
  - Wave 5 suite: 10 tasks (10 accepted, 0 failures)
  - Cumulative: $N=31$ tasks (30 accepted, 1 failure)
  - Acceptance rate: $96.77\%$ ($30/31$)
  - Wilson 95% lower bound: $0.8381$
  - Median successful-task latency: $25.5\text{s}$ (terminal median: $26.0\text{s}$)

- **Antigravity (`antigravity-native-executor`)**:
  - Baseline: 16 tasks (14 accepted, 2 failures: `tool_runtime`)
  - Wave 5 suite: 10 tasks (9 accepted, 1 failure: `tool_runtime`)
  - Cumulative: $N=26$ tasks (23 accepted, 3 failures)
  - Acceptance rate: $88.46\%$ ($23/26$)
  - Wilson 95% lower bound: $0.7102$
  - Median successful-task latency: $178.7\text{s}$ (terminal median: $186.6\text{s}$)

The advisory recommendation recommends `codex-native-executor` on the primary
reliability criterion: its Wilson lower bound ($0.8381$) exceeds Antigravity
($0.7102$). Furthermore, Codex displays substantially lower median successful-task
latency ($25.5\text{s}$ vs $178.7\text{s}$). The 10,000-iteration unpaired bootstrap
(seed 29) assigns `codex-native-executor` a $92.11\%$ first-rank probability.

### 8.2 Pre-evaluation completion vs. post-terminal independent feature quality

Wave 5 separated canonical in-task completion from post-terminal independent
quality:

| Evaluation Stage | Pairs | Both Passed | Codex Only | Antigravity Only | Neither |
|---|---:|---:|---:|---:|---:|
| **Pre-evaluation completion** | 10 | 9 | 1 | 0 | 0 |
| **Post-terminal quality** | 10 | 8 | 2 | 0 | 0 |

Post-terminal independent quality evaluated:
1. Runner-owned acceptance oracle (functional verification of the requested mutation)
2. Regression suite (proving existing repository invariants were preserved)
3. Neutral review (blinded prompt evaluated by `qwen / qwen2.5-coder:7b`)

For Codex: 10/10 passed acceptance, 10/10 passed regression, and 10/10 passed
neutral review (10/10 overall quality pass).
For Antigravity: 9/10 passed acceptance, 9/10 passed regression, and 8/10 passed
neutral review (8/10 overall quality pass; 1 quality fail due to terminal task failure
on `m29-mut-06-api-ratelimit-ag`, and 1 quality fail on
`m29-mut-04-cache-invalidation-ag` due to neutral review rejection).

### 8.3 Model vintage transition and operational report interpretation

Between Wave 4 (`2026-09-27`, Codex `gpt-5.6-luna/high`) and Wave 5 (`2026-10-25`,
Codex `gpt-6-luna/high`), the pinned Codex candidate execution identity transitioned.
Because the extractor is strictly fail-closed and enforces current-vintage identity
without backfill, the 37 earlier `gpt-5.6-luna` read-only tasks are categorized as
`execution_identity_mismatch` in the `2026-10-26` canonical snapshot, causing
`codex-native-executor-read-only` to register $N=0$ in this current-vintage cohort.
The operational diagnostic report (`evaluation/m29_provider_reliability_operational_report.{json,md}`)
preserves all historical model vintages across 523 scanned tasks (34 docs/read_only,
44 feature/read_only, 38 investigation/read_only, and 55 feature/mutation Codex tasks).

### 8.4 Longitudinal robustness split status

The robustness report remains `partial`. Model-aware execution identity
(`budget_usage.native_agent.model_execution`) was introduced on 2026-09-18 (37 days
prior to cutoff). Under the robustness policy, the historical split requires tasks
$\ge 45$ days old. Consequently, the historical 45-day window contains 0 verified
tasks, calendar-gating longitudinal consistency until approximately 2026-11-02 for
Wave 2/3 tasks and 2026-12-08 for `gpt-6-luna` tasks.

## 9. Final M29 4-cell qualification and milestone closeout

With Wave 5 published, all four canonical M29 task-class and mode cells have
empirical, verified advisory recommendations meeting or exceeding the 10-sample floor:

1. **`docs/read_only`**: recommends `codex-native-executor-read-only`
   (Wave 2/3: $N=10$ vs $10$, 10/10 vs 9/10 accepted)
2. **`feature/read_only`**: recommends `antigravity-native-executor-read-only`
   (Wave 3: $N=10$ vs $10$, 5/10 accepted each, lower successful latency $202.9\text{s}$ vs $359.4\text{s}$)
3. **`investigation/read_only`**: recommends `antigravity-native-executor-read-only`
   (Wave 4: $N=16$ Antigravity vs $N=17$ Codex, Wilson lower bound $0.7167$ vs $0.3096$)
4. **`feature/mutation`**: recommends `codex-native-executor`
   (Wave 5: $N=31$ Codex vs $N=26$ Antigravity, Wilson lower bound $0.8381$ vs $0.7102$,
   paired quality $10/10$ vs $8/10$, successful latency $25.5\text{s}$ vs $178.7\text{s}$)

### Final decision record

1. **Close out active M29 evidence collection and engineering**: All items under M29
   (1a registered references, 1b pre-dispatch diagnostics, 1c investigation qualification,
   1d post-terminal evaluation runner and 20-case mutation wave, and 1f hierarchical budget
   controls) are complete.
2. **Preserve fail-closed identity contracts**: No historical backfill or inference is
   permitted. Longitudinal consistency across the 45-day split is recognized as
   calendar-gated.
3. **Maintain static production routing**: M27 autonomy policy remains deferred. Production
   routing heuristics and `evaluation/routing_metrics.json` remain untouched.
4. **Advance active milestone priority**: Transition active focus to **M30 (GitHub-Native Task and Delivery Control)**.

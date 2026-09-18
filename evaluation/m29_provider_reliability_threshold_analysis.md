# M29 Provider Reliability Threshold Analysis

This document provides the empirical comparison, execution cohort analysis, and sensitivity evaluation across minimum-sample floors `5`, `10`, and `20` over a 90-day window (`2026-06-20` to `2026-09-18`), following the completion of the 20-task M29 Wave 2 live evidence collection and model-aware execution cohort isolation.

This analysis is strictly offline and advisory. **Production routing remains strictly unchanged (heuristic/static fallback); no live routing or `evaluation/routing_metrics.json` are modified.**

---

## 1. Executive Summary & Policy Decisions

Following the discovery in Wave 1 that OpenAI retired `gpt-5.4-mini` for ChatGPT-authenticated Codex users on August 31, 2026, the evaluation architecture was reshaped from naive profile grouping to authoritative, model-aware execution cohorts (`provider`, `model`, `reasoning_effort`) extracted directly from worker run budget usage metadata (`budget_usage["native_agent"]["model_execution"]`).

Two distinct versioned report views (schema v2) are published:
1. **Canonical Current-Cohort Advisory Report** (`evaluation/m29_provider_reliability_report.{json,md}`):
   - **Scope**: `current_execution_cohort`.
   - Strictly filters evidence to the verified, currently active provider configurations:
     - Codex: `gpt-5.6-luna` (reasoning effort: `high`)
     - Antigravity: `gemini-3.8-flash` (reasoning effort: `medium`)
   - Zero heuristic inference; tasks with missing, mixed, or mismatched models are excluded from aggregation.
   - **Floor Qualification**: Wave 2 powers `docs` (`read_only`) to achieve full dual-candidate qualification at the canonical sample floor ($N=10$ vs $10$):
     - **Rank 1**: `codex-native-executor-read-only` ($10/10 = 100\%$ accepted, Wilson 95% lower bound: $0.7225$, median latency: $194.6\text{s}$).
     - **Rank 2**: `antigravity-native-executor-read-only` ($9/10 = 90\%$ accepted, Wilson 95% lower bound: $0.5958$, median latency: $150.7\text{s}$).
     - **Advisory Recommendation**: `codex-native-executor-read-only`.
   - The other three canonical target cells (`feature/mutation`, `feature/read_only`, `investigation/read_only`) have zero tasks in the current execution cohort and report `insufficient_sample_size: 0 tasks (minimum 10)` with clean fallback reasons.
   - **Robustness Status**: Truthfully reported as `partial`. While the $N=10$ sample floor is met and 10,000 bootstrap iterations confirm a 66.1% win probability for Codex vs 33.9% for Antigravity, a single execution cluster cannot establish temporal split consistency or window sensitivity.
2. **Operational Diagnostic Report** (`evaluation/m29_provider_reliability_operational_report.{json,md}`):
   - **Scope**: `operational`.
   - Admits all valid historical tasks regardless of model vintage to provide complete 90-day system accounting, diagnostic failure taxonomy, Wave 1 deprecation documentation, and verifier delegation breakdown.
   - **Status**: Strictly `diagnostic_only`. Active provider recommendations and candidate rankings are suppressed (`recommended_profile: None`, `is_eligible: False`, `rank: None`) to prevent misleading head-to-head comparisons across heterogeneous model vintages.

---

## 2. Evidence Accounting & Provenance Audit

All evaluations were extracted with strict read-only transactions (`SET TRANSACTION READ ONLY`) against the Compose PostgreSQL database.

### Provenance Timestamps & Identities
- **Observation Reference (`as_of`)**: `2026-09-18T21:08:09Z`
- **Evidence Window**: `2026-06-20T21:08:09Z` to `2026-09-18T21:08:09Z` (90 days)
- **Wave 1 Diagnostic Baseline**: Preserved immutably at `artifacts/m29_evidence_bundle_wave1_diagnostic/bundle.json`.
- **Wave 2 Live Evidence Bundle**: Preserved at `artifacts/m29_evidence_bundle_wave2/bundle.json`.
- **Harness Build SHA**: `a97e96b993ade6156f29fa439982babf17549867`
- **Target Repository Revision**: `9eb38ed72b38f1aef41ac63ecc9c401da3acef2b` (`origin/master`)

### Accounting Reconciliation

| Metric | Canonical Current-Cohort View | Operational Diagnostic View |
|---|---|---|
| **Total Tasks Scanned** | 392 | 392 |
| **Included in Evidence** | 20 | 288 |
| **Excluded Tasks** | 372 | 104 |
| **Accounting Invariant** | $20 + 372 = 392$ | $288 + 104 = 392$ |

### Breakdown of Exclusions

| Reason | Canonical Count | Operational Count | Description |
|---|---|---|---|
| `unknown_execution_identity` | 268 | 0 | Tasks from legacy runs lacking authoritative `model_execution` budget metadata. |
| `non_temporal_runtime` | 81 | 81 | Pre-Temporal legacy tasks. |
| `malformed_inconsistent_timeline` | 10 | 10 | Tasks failing internal terminal timeline sequence validation. |
| `cancelled` | 9 | 9 | Tasks cancelled by operator prior to terminal outcome. |
| `evaluation_smoke` | 2 | 2 | Preflight smoke tasks tagged with `exclude_from_provider_reliability: True`. |
| `non_native_agent_mode` | 2 | 2 | Tasks executed in non-native agent modes. |

---

## 3. Catalog Profile Coverage

### Canonical Current-Cohort Coverage (`schema_version: 2`)

| Profile | Mode | Expected Cohort | Evidence Present | Total Samples | Eligible (Floor 10) |
|---|---|---|---|---|---|
| `codex-native-executor` | `mutation` | `codex:gpt-5.6-luna/high` | no | 0 | no |
| `codex-native-executor-read-only` | `read_only` | `codex:gpt-5.6-luna/high` | yes | 10 | **yes** |
| `antigravity-native-executor` | `mutation` | `antigravity:gemini-3.8-flash/medium` | no | 0 | no |
| `antigravity-native-executor-read-only` | `read_only` | `antigravity:gemini-3.8-flash/medium` | yes | 10 | **yes** |

### Operational View Coverage (Historical Across All Models)

| Profile | Mode | Evidence Present | Total Samples | Eligible (Floor 10) | Eligible (Floor 20) |
|---|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 38 | **yes** | **yes** |
| `codex-native-executor-read-only` | `read_only` | yes | 87 | **yes** | **yes** |
| `antigravity-native-executor` | `mutation` | yes | 83 | **yes** | **yes** |
| `antigravity-native-executor-read-only` | `read_only` | yes | 80 | **yes** | **yes** |

---

## 4. Cross-Threshold Comparison by Task Class

### 4.1. `docs` (read_only) — Canonical Current Cohort Focus

- **Floor 5 (Exploratory)**:
  - Rank 1: `codex-native-executor-read-only` ($N=10$, Acc=10, Wilson Lower=0.7225, Med Latency=194.6s)
  - Rank 2: `antigravity-native-executor-read-only` ($N=10$, Acc=9, Wilson Lower=0.5958, Med Latency=150.7s)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 10 (Canonical Floor — Satisfied)**:
  - Rank 1: `codex-native-executor-read-only` ($N=10$, Acc=10, Wilson Lower=0.7225)
  - Rank 2: `antigravity-native-executor-read-only` ($N=10$, Acc=9, Wilson Lower=0.5958)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Both candidates have $N=10 < 20$.
  - Fallback: `no_eligible_candidates: all candidates lack sufficient samples`
  - Recommendation: _None_
- **Assessment**:
  Wave 2 successfully delivers 20 live docs tasks (10 paired topics across Codex and Antigravity) executing under verified current-cohort credentials. Codex achieved 10/10 completed runs with zero errors. Antigravity achieved 9/10 completed runs (1 failure due to independent verifier delegation). Codex ranks #1 on Wilson lower bound ($0.7225$ vs $0.5958$) and wins in 66.1% of bootstrap iterations.

### 4.2. Other Task Classes in Canonical Cohort View

The canonical M29 target groups encompass four specific cells:
- `docs` (read_only): Qualified ($N=10$ vs $10$) $\rightarrow$ Recommended: `codex-native-executor-read-only`.
- `feature` (mutation): $N=0$ in current cohort $\rightarrow$ fallback: `no_eligible_candidates`.
- `feature` (read_only): $N=0$ in current cohort $\rightarrow$ fallback: `no_eligible_candidates`.
- `investigation` (read_only): $N=0$ in current cohort $\rightarrow$ fallback: `no_eligible_candidates`.

### 4.3. Operational 90-Day Diagnostic Perspective

In the operational diagnostic view (aggregating across all model vintages over the 90-day window):
- **Report Status**: `diagnostic_only`.
- Active recommendations are suppressed (`recommended_profile: None`, `rank: None`, `is_eligible: False`) with explicit fallback reason `diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort`.
- Historical sample sizes and raw pass rates remain fully inspectable for systems diagnostics:
  - `feature` (mutation): $N=74$ Antigravity ($67.7\%$ lower bound), $N=25$ Codex ($44.5\%$ lower bound).
  - `feature` (read_only): $N=34$ Codex ($53.8\%$ lower bound), $N=30$ Antigravity ($45.5\%$ lower bound).
  - `investigation` (read_only): $N=15$ Codex ($79.6\%$ lower bound), $N=12$ Antigravity ($46.8\%$ lower bound).

---

## 5. Failure Mode & Provider Diagnostics

### Wave 1 vs Wave 2 Diagnostic Comparison

1. **Wave 1 Deprecation Event (September 17, 2026)**:
   - All 8 Codex read-only tasks failed with `worker_failure` (exit code 400) due to OpenAI's retirement of `gpt-5.4-mini` for ChatGPT accounts on August 31, 2026.
   - 2 Antigravity docs tasks failed with `infra_verifier_unavailable` when independent verification delegated to the unavailable Codex mini model.
   - Preserved as diagnostic baseline at `artifacts/m29_evidence_bundle_wave1_diagnostic/`.
2. **Wave 2 Reshaped Execution (September 18–19, 2026)**:
   - Updated Codex configuration to `gpt-5.6-luna` (high reasoning effort) and Antigravity to `gemini-3.8-flash` (medium reasoning effort).
   - Preflight smoke verification validated live container model resolution and proved early exclusion of smoke tasks as `evaluation_smoke`.
   - Codex read-only executed 10/10 tasks cleanly ($100\%$ acceptance, zero errors, median duration $194.6\text{s}$).
   - Antigravity read-only executed 9/10 tasks cleanly ($90\%$ acceptance, median duration $150.7\text{s}$). The single failure on `m29-w2-docs-05` cleanly resolved to `infra_verifier_unavailable` via timeline failure event precedence over worker run verifier outcome.

---

## 6. Conclusions & Change Controls

1. **Statistically Truthful Delivery**:
   - Model-aware execution cohort filtering guarantees that evidence cells represent uniform, verified runtime configurations.
   - Upstream cohort filtering guarantees that two models for the same profile cannot both enter the candidate pool and artificially satisfy the $\ge 2$ candidates rule.
   - Wave 2 qualifies `docs/read_only` at the canonical sample floor ($N=10$), while robustness status is explicitly reported as `partial` to acknowledge that temporal consistency cannot be claimed from a single timestamp cluster.
2. **Production Routing Integrity**:
   - **Production routing remains strictly unchanged.** Production routing continues to use the existing static/checked-in metrics. Any runtime routing update is reserved for future explicit policy slices with full change controls.

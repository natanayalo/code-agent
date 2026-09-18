# M29 Provider Reliability Threshold Analysis

This document provides the empirical comparison and sensitivity analysis across minimum-sample floors `5`, `10`, and `20` from the local Compose PostgreSQL evidence snapshot taken at `2026-09-17T20:43:50Z` over a 90-day window (`2026-06-19T20:43:50Z` to `2026-09-17T20:43:50Z`), following the completion of the 28-task M29 live evidence wave.

This analysis is strictly offline and advisory. **No live runtime routing, report schemas, or `evaluation/routing_metrics.json` are modified.**

---

## 1. Executive Summary & Policy Decisions

Following the completion of the frozen 28-task live evidence wave against the real `code-agent` repository, all four target operational cells now possess dual-candidate empirical evidence meeting the canonical floor 10, and three cells meet the conservative floor 20:

- **Recommend a profile only when $\ge 2$ compatible profiles meet the selected floor** (enforced by schema-v1 contract).
- **Stricter floor eligibility check (`feature` mutation, `feature` read_only, `docs` read_only)**:
  - **Recommendations remain fully available under the stricter eligibility floor of 20** for:
    - `feature` (mutation): `antigravity-native-executor` ($N=74$, Wilson lower: $0.6773$).
    - `feature` (read_only): `codex-native-executor-read-only` ($N=34$, Wilson lower: $0.5383$, vs. Antigravity $N=30$, Wilson lower: $0.4551$).
    - `docs` (read_only): `antigravity-native-executor-read-only` ($N=28$, Wilson lower: $0.3263$, vs. Codex $N=28$, Wilson lower: $0.1525$).
- **Eligible at 10 but not 20 $\rightarrow$ retain 10 provisionally and label the conservative view insufficient**:
  - Applies to: `investigation` (read_only).
    - Floor 10 recommends `codex-native-executor-read-only` (Codex $N=15$, Wilson lower: $0.7961$, Antigravity $N=12$, Wilson lower: $0.4677$).
    - At floor 20, neither profile meets the floor ($N < 20$), falling back to `no_eligible_candidates`.
    - Following the decision rule, the recommendation is retained at the canonical floor 10 and labeled insufficient under the conservative floor 20.
- **Robustness Sensitivity Integration**:
  - 10,000 bootstrap iterations (seed 29) confirm high ranking stability across the recommended profiles:
    - `investigation` (read_only): Codex wins in 100% of bootstrap iterations.
    - `feature` (mutation): Antigravity wins in 98.0% of bootstrap iterations.
    - `docs` (read_only): Antigravity wins in 96.0% of bootstrap iterations.
    - `feature` (read_only): Codex wins in 74.9% of bootstrap iterations.

---

## 2. Evidence Accounting & Provenance Audit

Extraction was conducted with strict read-only transactions (`SET TRANSACTION READ ONLY`) against the local Compose PostgreSQL volume.

### Provenance Timestamps
- **Observation Reference (`as_of`)**: `2026-09-17T20:43:50Z`
- **Evidence Window**: `2026-06-19T20:43:50Z` to `2026-09-17T20:43:50Z` (90 days)
- **Extraction Generated At**: `2026-09-17T20:44:45Z`
- **Committed Harness Build SHA**: `bac0c57ddd302f524f1e3b84e0fc5b6de82b85fc`
- **Target Repository Revision**: `32d770eaef1addb134b744c5660dc5f56e2aed19` (`origin/master`)

### Accounting Reconciliation
- **Total Tasks Scanned**: 370
- **Included in Evidence**: 268
- **Excluded Tasks**: 102
- **Accounting Invariant**: $268 + 102 = 370$ (reconciled exactly)

### Breakdown of Exclusions
| Reason | Count | Notes |
|---|---|---|
| `non_temporal_runtime` | 81 | Historical tasks executed under pre-Temporal or legacy runtimes. |
| `malformed_inconsistent_timeline` | 10 | Tasks failing internal timeline sequence consistency validation. |
| `cancelled` | 9 | Tasks terminated by user cancellation before terminal outcome. |
| `non_native_agent_mode` | 2 | Tasks executed with non-native agent execution modes. |

*Zero exclusions were introduced by the 28-task live evidence wave.* All 28 tasks cleanly satisfied runtime, mode, and timeline consistency checks.

### Sanitization & Redaction Verification
The tracked outputs (`evaluation/m29_provider_reliability_report.{json,md}` and `evaluation/m29_provider_reliability_robustness_report.{json,md}`) were audited:
- Validated via `assert_sanitized_report()` and `assert_sanitized_robustness_report()`.
- Zero private task identifiers, prompt text, user/repo URLs, branch names, execution logs, artifact paths, or secrets are present.
- All identifiers conform to strictly allowlisted schema keys and safe regex patterns.

---

## 3. Catalog Profile Coverage Across Floors

| Profile | Mode | Total Samples | Eligible at Floor 5 | Eligible at Floor 10 | Eligible at Floor 20 |
|---|---|---|---|---|---|
| `antigravity-native-executor` | `mutation` | 83 | **yes** | **yes** | **yes** |
| `codex-native-executor` | `mutation` | 38 | **yes** | **yes** | **yes** |
| `codex-native-executor-read-only` | `read_only` | 77 | **yes** | **yes** | **yes** |
| `antigravity-native-executor-read-only` | `read_only` | 70 | **yes** | **yes** | **yes** |

All four catalog profiles have now crossed the conservative threshold of 20 total samples repository-wide.

---

## 4. Cross-Threshold Comparison by Task Class

### 4.1. `feature` (mutation)
- **Floor 5 (Exploratory)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Acc=58, Wilson Lower=0.6773, Med Latency=43.9s, Recency=2.0d)
  - Rank 2: `codex-native-executor` ($N=25$, Acc=16, Wilson Lower=0.4452, Med Latency=102.8s, Recency=2.2d)
  - Recommendation: `antigravity-native-executor`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Wilson Lower=0.6773)
  - Rank 2: `codex-native-executor` ($N=25$, Wilson Lower=0.4452)
  - Recommendation: `antigravity-native-executor`
- **Floor 20 (Conservative Sensitivity)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Wilson Lower=0.6773)
  - Rank 2: `codex-native-executor` ($N=25$, Wilson Lower=0.4452)
  - Recommendation: `antigravity-native-executor`
- **Assessment**: **Recommendation remains available under stricter floor 20** ($N=74$ and $N=25$). Antigravity exhibits a significantly higher Wilson lower bound ($0.6773$ vs $0.4452$) and faster median completion latency ($43.9\text{s}$ vs $102.8\text{s}$), with 98.0% bootstrap win probability.

### 4.2. `feature` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `codex-native-executor-read-only` ($N=34$, Acc=24, Wilson Lower=0.5383, Med Latency=153.9s, Recency=12.2d)
  - Rank 2: `antigravity-native-executor-read-only` ($N=30$, Acc=19, Wilson Lower=0.4551, Med Latency=38.1s, Recency=0.0d)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `codex-native-executor-read-only` ($N=34$, Wilson Lower=0.5383)
  - Rank 2: `antigravity-native-executor-read-only` ($N=30$, Wilson Lower=0.4551)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Rank 1: `codex-native-executor-read-only` ($N=34$, Wilson Lower=0.5383)
  - Rank 2: `antigravity-native-executor-read-only` ($N=30$, Wilson Lower=0.4551)
  - Recommendation: `codex-native-executor-read-only`
- **Assessment**: **Fully qualified at conservative floor 20**. Following the 10 Antigravity feature read-only tasks collected in the live wave (all 10 completed cleanly), Antigravity increased from $N=10$ to $N=30$, qualifying both profiles at floor 20. Codex retains Rank 1 due to higher historical accepted rate (70.6% vs 63.3%, Wilson lower $0.5383$ vs $0.4551$), despite Antigravity\x27s faster median latency ($38.1\text{s}$ vs $153.9\text{s}$). Bootstrap win probability: Codex 74.9%, Antigravity 25.1%.

### 4.3. `docs` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=28$, Acc=14, Wilson Lower=0.3263, Med Latency=45.0s, Recency=0.0d)
  - Rank 2: `codex-native-executor-read-only` ($N=28$, Acc=8, Wilson Lower=0.1525, Med Latency=21.7s, Recency=0.0d)
  - Recommendation: `antigravity-native-executor-read-only`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=28$, Wilson Lower=0.3263)
  - Rank 2: `codex-native-executor-read-only` ($N=28$, Wilson Lower=0.1525)
  - Recommendation: `antigravity-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=28$, Wilson Lower=0.3263)
  - Rank 2: `codex-native-executor-read-only` ($N=28$, Wilson Lower=0.1525)
  - Recommendation: `antigravity-native-executor-read-only`
- **Assessment**: **Fully qualified at conservative floor 20**. Following the 16 paired docs read-only tasks (8 Antigravity, 8 Codex), both candidates reached $N=28$. Antigravity achieved a higher accepted rate (50.0% vs 28.6%, Wilson lower $0.3263$ vs $0.1525$), winning in 96.0% of bootstrap iterations.

### 4.4. `investigation` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `codex-native-executor-read-only` ($N=15$, Acc=15, Wilson Lower=0.7961, Med Latency=136.9s, Recency=12.2d)
  - Rank 2: `antigravity-native-executor-read-only` ($N=12$, Acc=9, Wilson Lower=0.4677, Med Latency=78.8s, Recency=0.1d)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `codex-native-executor-read-only` ($N=15$, Wilson Lower=0.7961)
  - Rank 2: `antigravity-native-executor-read-only` ($N=12$, Wilson Lower=0.4677)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Ineligible: Both candidates ($N=15 < 20$, $N=12 < 20$)
  - Fallback: `no_eligible_candidates: all candidates lack sufficient samples`
  - Recommendation: _None_
- **Assessment**: **Dual-candidate qualification established at floor 10**. With 2 Antigravity tasks added in the live wave, Antigravity reached $N=12$, satisfying the 10-sample floor. Codex achieves Rank 1 with 100% acceptance ($15/15$, Wilson lower $0.7961$), winning in 100.0% of bootstrap iterations. Under the conservative floor 20, both candidates remain insufficient.

### 4.5. Other Task Classes (Insufficient Across All Floors)
- `bugfix` (mutation): Codex has $N=5$, Antigravity has $N=2$. Both $< 10$.
- `docs` (mutation): Codex has $N=3$, Antigravity has $N=2$. Both $< 10$.
- `investigation` (mutation): Codex has $N=4$, Antigravity has $N=0$.
- `maintenance` (mutation): Antigravity has $N=5$, Codex has $N=0$.
- `refactor` (mutation): Codex has $N=1$, Antigravity has $N=0$.
- `scout` (read_only): $N=0$ for both candidates.

---

## 5. Failure Mode & Provider Diagnostics from the Live Wave

The live wave yielded critical diagnostic insights into real provider execution under production-like settings:

1. **Codex Account/Model Incompatibility**:
   - All 8 Codex read-only tasks in the live wave failed rapidly with `worker_failure` (exit code 400):
     `The \x27gpt-5.4-mini\x27 model is not supported when using Codex with a ChatGPT account.`
   - These terminal failures reflect genuine worker execution errors under the configured credentials/model and were cleanly captured by the verification stage as `worker_failure`.
2. **Antigravity High Feature Capability**:
   - All 10 Antigravity `feature` (read_only) implementation proposal tasks completed successfully ($10/10 = 100\%$), producing structured Markdown proposals with zero file modifications and zero permission checks triggered.
3. **Antigravity Verifier Delegation & Permission Boundaries**:
   - In `m29-docs-05` and `m29-docs-07`, Antigravity workers completed successfully, but independent verification delegated to Codex failed due to the aforementioned model error, surfacing as `infra_verifier_unavailable`.
   - In `m29-inv-01`, Antigravity attempted to inspect `agent-home/.gemini/antigravity-cli/brain`, which correctly tripped the hardcoded sandbox protection boundary rule.

---

## 6. Manual Overrides and Routing Audit

Manual override rates across eligible cells:
- `feature` (mutation): Antigravity 74/74 (100%), Codex 25/25 (100%)
- `feature` (read_only): Antigravity 29/30 (96.7%), Codex 34/34 (100%)
- `docs` (read_only): Antigravity 28/28 (100%), Codex 28/28 (100%)
- `investigation` (read_only): Antigravity 12/12 (100%), Codex 15/15 (100%)
- `bugfix` (mutation): Antigravity 1/2 (50%), Codex 5/5 (100%)

**Key Takeaway**:
Nearly all native-agent executions in the 90-day window were explicit operator overrides. The advisory metrics reflect empirical provider execution reliability when assigned, not automated routing performance.

---

## 7. Conclusions & Next Steps

1. **M29 Milestones Achieved**:
   - Canonical floor 10 recommendations now exist across all 4 operational cells (`feature` mutation, `feature` read_only, `docs` read_only, `investigation` read_only).
   - Conservative floor 20 recommendations exist across 3 operational cells.
   - Comprehensive robustness baseline (window variation, split-half cohorts, 10,000 bootstrap iterations) is fully established and sanitized.
2. **Production Routing Remains Unchanged**:
   - Production routing continues to use the existing static/checked-in metrics. Any runtime routing update is reserved for future explicit policy slices.

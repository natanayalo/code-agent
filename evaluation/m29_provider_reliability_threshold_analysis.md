# M29 Provider Reliability Threshold Analysis

This document provides the empirical comparison and sensitivity analysis across minimum-sample floors `5`, `10`, and `20` from the local Compose PostgreSQL evidence snapshot taken at `2026-09-17T10:10:00Z` over a 90-day window (`2026-06-19T10:10:00Z` to `2026-09-17T10:10:00Z`).

This analysis is strictly offline and advisory. **No live runtime routing, report schemas, or `evaluation/routing_metrics.json` are modified.**

---

## 1. Executive Summary & Policy Decisions

In accordance with the agreed decision rules:
- **Recommend a profile only when $\ge 2$ compatible profiles meet the selected floor** (enforced by schema-v1 contract).
- **Stable at floors 10 and 20 $\rightarrow$ retain 10 as the proposed evidence floor and record the 20-sample result as confirmation.**
  - Applies to: `feature` (mutation). Both floor 10 and floor 20 recommend `antigravity-native-executor` over `codex-native-executor`.
- **Eligible at 10 but not 20 $\rightarrow$ retain 10 provisionally and label the conservative view insufficient.**
  - Applies to:
    - `feature` (read_only): Floor 10 recommends `antigravity-native-executor-read-only`. At floor 20, only `codex-native-executor-read-only` has sufficient samples ($N=34$), while Antigravity ($N=10$) does not, causing a fallback due to insufficient candidates.
    - `docs` (read_only): Floor 10 recommends `antigravity-native-executor-read-only` (resolved by latency tie-break with identical Wilson lower bounds). At floor 20, neither candidate meets the floor ($N=12$ each).
- **Eligible only at 5 $\rightarrow$ retain 10-sample default and publish `insufficient_data`; do not lower the policy merely to obtain a recommendation.**
  - Applies to: `investigation` (read_only). At floor 5, Codex ($N=15$) and Antigravity ($N=8$) are both eligible, recommending `codex-native-executor-read-only`. At floor 10, Antigravity has only 8 samples, leaving only 1 eligible candidate and triggering fallback. The 10-sample default is maintained, publishing no recommendation for this class.
- **Conflicting eligible rankings $\rightarrow$ publish no profile recommendation.**
  - No conflicting rankings were observed across any eligible threshold runs.

---

## 2. Evidence Accounting & Sanitization Audit

Extraction was conducted with strict read-only transactions (`SET TRANSACTION READ ONLY`) against the local Compose PostgreSQL volume.

### Accounting Reconciliation
- **Total Tasks Scanned**: 314
- **Included in Evidence**: 212
- **Excluded Tasks**: 102
- **Accounting Invariant**: $212 + 102 = 314$ (reconciled exactly)

### Breakdown of Exclusions
| Reason | Count | Notes |
|---|---|---|
| `non_temporal_runtime` | 81 | Historical tasks executed under pre-Temporal or legacy runtimes. |
| `malformed_inconsistent_timeline` | 10 | Tasks failing internal timeline sequence consistency validation. |
| `cancelled` | 9 | Tasks terminated by user cancellation before terminal outcome. |
| `non_native_agent_mode` | 2 | Tasks executed with non-native agent execution modes. |

### Sanitization & Redaction Verification
The tracked outputs (`evaluation/m29_provider_reliability_report.{json,md}`) were audited:
- Validated via `assert_sanitized_report()`.
- Zero private task identifiers, prompt text, user/repo URLs, branch names, execution logs, artifact paths, or secrets are present.
- All identifiers conform to strictly allowlisted schema keys and safe regex patterns.

---

## 3. Catalog Profile Coverage Across Floors

| Profile | Mode | Total Samples | Eligible at Floor 5 | Eligible at Floor 10 | Eligible at Floor 20 |
|---|---|---|---|---|---|
| `antigravity-native-executor` | `mutation` | 83 | **yes** | **yes** | **yes** |
| `codex-native-executor` | `mutation` | 38 | **yes** | **yes** | **yes** |
| `codex-native-executor-read-only` | `read_only` | 61 | **yes** | **yes** | **yes** |
| `antigravity-native-executor-read-only` | `read_only` | 30 | **yes** | **yes** | **no** (max cell $N=12$) |

---

## 4. Cross-Threshold Comparison by Task Class

### 4.1. `feature` (mutation)
- **Floor 5 (Exploratory)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Acc=58, Wilson Lower=0.6773, Med Latency=43.9s, Recency=1.5d)
  - Rank 2: `codex-native-executor` ($N=25$, Acc=16, Wilson Lower=0.4452, Med Latency=102.8s, Recency=1.7d)
  - Recommendation: `antigravity-native-executor`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Wilson Lower=0.6773)
  - Rank 2: `codex-native-executor` ($N=25$, Wilson Lower=0.4452)
  - Recommendation: `antigravity-native-executor`
- **Floor 20 (Conservative Sensitivity)**:
  - Rank 1: `antigravity-native-executor` ($N=74$, Wilson Lower=0.6773)
  - Rank 2: `codex-native-executor` ($N=25$, Wilson Lower=0.4452)
  - Recommendation: `antigravity-native-executor`
- **Assessment**: **Confirmed stable**. Retain floor 10 with strong confirmation from floor 20. Both candidates have substantial volume, high recency ($< 2$ days), and Antigravity exhibits superior lower-bound reliability and faster median completion latency.

### 4.2. `feature` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=10$, Acc=9, Wilson Lower=0.5958, Med Latency=38.1s, Recency=11.8d)
  - Rank 2: `codex-native-executor-read-only` ($N=34$, Acc=24, Wilson Lower=0.5383, Med Latency=153.9s, Recency=11.8d)
  - Recommendation: `antigravity-native-executor-read-only`
- **Floor 10 (Candidate Default)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=10$, Wilson Lower=0.5958)
  - Rank 2: `codex-native-executor-read-only` ($N=34$, Wilson Lower=0.5383)
  - Recommendation: `antigravity-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Eligible: `codex-native-executor-read-only` ($N=34$)
  - Ineligible: `antigravity-native-executor-read-only` ($N=10 < 20$)
  - Fallback: `insufficient_eligible_candidates: only 1 eligible candidate ('codex-native-executor-read-only') available; at least 2 required`
  - Recommendation: _None_
- **Assessment**: **Provisional at floor 10**. Conservative view is insufficient due to Antigravity's sample count ($N=10$). Collecting 10 additional Antigravity read-only feature tasks will enable conservative floor 20 evaluation.

### 4.3. `docs` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=12$, Acc=8, Wilson Lower=0.3906, Med Latency=45.0s, Recency=10.1d)
  - Rank 2: `codex-native-executor-read-only` ($N=12$, Acc=8, Wilson Lower=0.3906, Med Latency=210.8s, Recency=11.5d)
  - Recommendation: `antigravity-native-executor-read-only` (latency tie-breaker)
- **Floor 10 (Candidate Default)**:
  - Rank 1: `antigravity-native-executor-read-only` ($N=12$, Wilson Lower=0.3906, Med Latency=45.0s)
  - Rank 2: `codex-native-executor-read-only` ($N=12$, Wilson Lower=0.3906, Med Latency=210.8s)
  - Recommendation: `antigravity-native-executor-read-only`
- **Floor 20 (Conservative Sensitivity)**:
  - Ineligible: Both candidates ($N=12 < 20$)
  - Fallback: `no_eligible_candidates`
  - Recommendation: _None_
- **Assessment**: **Provisional at floor 10**. Both candidates have identical acceptance rates (8/12 = 66.7%, Wilson interval $[0.3906, 0.8647]$); the recommendation relies on the median latency tie-breaker. Conservative floor 20 is insufficient; 8 additional samples per profile are required.

### 4.4. `investigation` (read_only)
- **Floor 5 (Exploratory)**:
  - Rank 1: `codex-native-executor-read-only` ($N=15$, Acc=15, Wilson Lower=0.7961, Med Latency=136.9s, Recency=11.8d)
  - Rank 2: `antigravity-native-executor-read-only` ($N=8$, Acc=8, Wilson Lower=0.6756, Med Latency=78.8s, Recency=33.1d)
  - Recommendation: `codex-native-executor-read-only`
- **Floor 10 (Candidate Default)**:
  - Eligible: `codex-native-executor-read-only` ($N=15$)
  - Ineligible: `antigravity-native-executor-read-only` ($N=8 < 10$)
  - Fallback: `insufficient_eligible_candidates: only 1 eligible candidate ('codex-native-executor-read-only') available; at least 2 required`
  - Recommendation: _None_
- **Floor 20 (Conservative Sensitivity)**:
  - Ineligible: Both candidates ($N=15, 8 < 20$)
  - Fallback: `no_eligible_candidates`
  - Recommendation: _None_
- **Assessment**: **Retain floor 10 default; publish no recommendation**. While both profiles achieved 100% acceptance ($15/15$ and $8/8$), Antigravity is 2 samples short of floor 10. Per decision rules, we do not lower the policy threshold to 5 merely to generate a recommendation. Only 2 additional Antigravity investigation tasks are needed to achieve floor 10 qualification.

### 4.5. Other Task Classes (Insufficient Across All Floors)
- `bugfix` (mutation): Codex has $N=5$, Antigravity has $N=2$. Both $< 10$.
- `docs` (mutation): Codex has $N=3$, Antigravity has $N=2$. Both $< 10$.
- `investigation` (mutation): Codex has $N=4$, Antigravity has $N=0$.
- `maintenance` (mutation): Antigravity has $N=5$, Codex has $N=0$.
- `refactor` (mutation): Codex has $N=1$, Antigravity has $N=0$.
- `scout` (read_only): $N=0$ for both candidates.

All of these classes fall back to `no_eligible_candidates` or `insufficient_eligible_candidates` across all floors.

---

## 5. Manual Overrides and Routing Audit

Manual override rates were tracked across all cells:
- `feature` (mutation): Antigravity 74/74 (100%), Codex 25/25 (100%)
- `feature` (read_only): Antigravity 9/10 (90%), Codex 34/34 (100%)
- `docs` (read_only): Antigravity 12/12 (100%), Codex 12/12 (100%)
- `investigation` (read_only): Antigravity 8/8 (100%), Codex 15/15 (100%)
- `bugfix` (mutation): Antigravity 1/2 (50%), Codex 5/5 (100%)

**Critical Observation**:
Nearly 100% of historical native-agent executions in this observation window resulted from explicit operator overrides rather than automated capability routing. These metrics therefore represent empirical worker reliability under task execution, rather than the efficacy of autonomous routing decisions.

---

## 6. Target Evidence Needs for Next Phase

To promote provisional floor 10 recommendations to confirmed floor 20 status and close high-priority eligibility gaps:

1. **`investigation` (read_only)**: Collect **2 additional Antigravity tasks** to reach $N=10$ and establish a dual-candidate floor 10 recommendation. (For floor 20: 5 Codex, 12 Antigravity).
2. **`feature` (read_only)**: Collect **10 additional Antigravity tasks** to promote to confirmed floor 20 status.
3. **`docs` (read_only)**: Collect **8 additional tasks for both profiles** to promote to confirmed floor 20 status.
4. **`bugfix` (mutation)**: Collect **8 Antigravity and 5 Codex tasks** to reach floor 10 eligibility.

# M29 Provider Reliability Robustness Report

- **Status**: `partial`
- **Evidence Scope**: `current_execution_cohort`
- **Generated At**: `2026-10-25T10:47:59.010126+00:00`
- **Observation Reference (`as_of`)**: `2026-10-26T09:12:04.572097+00:00`
- **Lookback Window**: 90 days (`2026-07-28T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00`)
- **Minimum Samples Per Cell**: 10
- **Bootstrap Resampling**: 10,000 iterations, seed 29

## 1. Executive Robustness Summary

| Task Class | Mode | 30d Window | 60d Window | 90d Window | Hist (90d-45d) | Recent (45d-0d) | Bootstrap Winner | Win Prob |
|---|---|---|---|---|---|---|---|---|
| `docs` | `read_only` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `feature` | `mutation` | `codex-native-executor` | `codex-native-executor` | `codex-native-executor` | `None` | `codex-native-executor` | `codex-native-executor` | 92% |
| `feature` | `read_only` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `investigation` | `read_only` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |

## 2. Window Variation Analysis

### 30-Day Lookback Window (`2026-09-26T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00`)
- **Included Tasks**: 67

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (mutation)**: Recommended: `codex-native-executor`
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

### 60-Day Lookback Window (`2026-08-27T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00`)
- **Included Tasks**: 93

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`feature` (mutation)**: Recommended: `codex-native-executor`
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

### 90-Day Lookback Window (`2026-07-28T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00`)
- **Included Tasks**: 93

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`feature` (mutation)**: Recommended: `codex-native-executor`
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

## 3. Temporal Split-Half Cohorts

Evaluates temporal stability across non-overlapping partitions of the 90-day window: historical `[as_of-90d, as_of-45d)` and recent `[as_of-45d, as_of]`.

### Cohort: `historical` (`2026-07-28T09:12:04.572097+00:00` to `2026-09-11T09:12:04.572097+00:00`)
- **Included Tasks**: 0

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### Cohort: `recent` (`2026-09-11T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00`)
- **Included Tasks**: 93

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`feature` (mutation)**: Recommended: `codex-native-executor`
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

## 4. Bootstrap Resampling Sensitivity

Evaluates 10,000 deterministic bootstrap iterations (seed 29) resampling whole task observations independently per profile cell. Successful-task latency is the ranking tie-break metric; failure latency remains an operational metric in the canonical report. A separate Wave 3 paired supplement preserves topic-pair membership. Advisory-only descriptive results; no automated routing threshold is applied.

### Group: `docs` (read_only)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 1 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `feature` (mutation)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `codex-native-executor` | 31 | 9211 | 0.9211 | R1:9211, R2:789 | R1:0.9211, R2:0.0789 |
| `antigravity-native-executor` | 26 | 789 | 0.0789 | R1:789, R2:9211 | R1:0.0789, R2:0.9211 |

### Group: `feature` (read_only)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 1 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `investigation` (read_only)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 1 eligible profile(s) meet the sample floor (10); at least 2 required

## 5. Evidence Accounting & 90-Day Snapshot Exclusions

Accounting metrics reflect the full 90-day observation snapshot scanned from the database.

- **Total Tasks Scanned**: 523
- **Included In 90-Day Evidence**: 93
- **Excluded Tasks**: 430

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 10 |
| `execution_identity_mismatch` | 37 |
| `malformed_inconsistent_timeline` | 1 |
| `non_native_agent_mode` | 2 |
| `outside_window` | 115 |
| `unknown_execution_identity` | 256 |

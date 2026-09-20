# M29 Provider Reliability Robustness Report

- **Status**: `partial`
- **Evidence Scope**: `current_execution_cohort`
- **Generated At**: `2026-09-19T23:37:50.307178+00:00`
- **Observation Reference (`as_of`)**: `2026-09-19T21:32:22.562107+00:00`
- **Lookback Window**: 90 days (`2026-06-21T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00`)
- **Minimum Samples Per Cell**: 10
- **Bootstrap Resampling**: 10,000 iterations, seed 29

## 1. Executive Robustness Summary

| Task Class | Mode | 30d Window | 60d Window | 90d Window | Hist (90d-45d) | Recent (45d-0d) | Bootstrap Winner | Win Prob |
|---|---|---|---|---|---|---|---|---|
| `docs` | `read_only` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | `None` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | 66% |
| `feature` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `feature` | `read_only` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | `None` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | 59% |
| `investigation` | `read_only` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |

## 2. Window Variation Analysis

### 30-Day Lookback Window (`2026-08-20T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00`)
- **Included Tasks**: 55

- **`docs` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### 60-Day Lookback Window (`2026-07-21T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00`)
- **Included Tasks**: 55

- **`docs` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### 90-Day Lookback Window (`2026-06-21T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00`)
- **Included Tasks**: 55

- **`docs` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

## 3. Temporal Split-Half Cohorts

Evaluates temporal stability across non-overlapping partitions of the 90-day window: historical `[as_of-90d, as_of-45d)` and recent `[as_of-45d, as_of]`.

### Cohort: `historical` (`2026-06-21T21:32:22.562107+00:00` to `2026-08-05T21:32:22.562107+00:00`)
- **Included Tasks**: 0

- **`docs` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### Cohort: `recent` (`2026-08-05T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00`)
- **Included Tasks**: 55

- **`docs` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

## 4. Bootstrap Resampling Sensitivity

Evaluates 10,000 deterministic bootstrap iterations (seed 29) resampling whole task observations independently per profile cell. Successful-task latency is the ranking tie-break metric; failure latency remains an operational metric in the canonical report. A separate Wave 3 paired supplement preserves topic-pair membership. Advisory-only descriptive results; no automated routing threshold is applied.

### Group: `docs` (read_only)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `codex-native-executor-read-only` | 10 | 6606 | 0.6606 | R1:6606, R2:3394 | R1:0.6606, R2:0.3394 |
| `antigravity-native-executor-read-only` | 10 | 3394 | 0.3394 | R1:3394, R2:6606 | R1:0.3394, R2:0.6606 |

### Group: `feature` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `feature` (read_only)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `antigravity-native-executor-read-only` | 10 | 5861 | 0.5861 | R1:5861, R2:4139 | R1:0.5861, R2:0.4139 |
| `codex-native-executor-read-only` | 10 | 4139 | 0.4139 | R1:4139, R2:5861 | R1:0.4139, R2:0.5861 |

### Group: `investigation` (read_only)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

## 5. Evidence Accounting & 90-Day Snapshot Exclusions

Accounting metrics reflect the full 90-day observation snapshot scanned from the database.

- **Total Tasks Scanned**: 434
- **Included In 90-Day Evidence**: 55
- **Excluded Tasks**: 379

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 4 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |
| `unknown_execution_identity` | 273 |

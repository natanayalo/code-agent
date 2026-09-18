# M29 Provider Reliability Robustness Report

- **Status**: `partial`
- **Generated At**: `2026-09-17T20:44:56.615867+00:00`
- **Observation Reference (`as_of`)**: `2026-09-17T20:43:50+00:00`
- **Lookback Window**: 90 days (`2026-06-19T20:43:50+00:00` to `2026-09-17T20:43:50+00:00`)
- **Minimum Samples Per Cell**: 10
- **Bootstrap Resampling**: 10,000 iterations, seed 29

## 1. Executive Robustness Summary

| Task Class | Mode | 30d Window | 60d Window | 90d Window | Hist (90d-45d) | Recent (45d-0d) | Bootstrap Winner | Win Prob |
|---|---|---|---|---|---|---|---|---|
| `bugfix` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `docs` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `docs` | `read_only` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | `None` | `antigravity-native-executor-read-only` | `antigravity-native-executor-read-only` | 96% |
| `feature` | `mutation` | `None` | `antigravity-native-executor` | `antigravity-native-executor` | `None` | `antigravity-native-executor` | `antigravity-native-executor` | 98% |
| `feature` | `read_only` | `None` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | `None` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | 75% |
| `investigation` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `investigation` | `read_only` | `None` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | `None` | `codex-native-executor-read-only` | `codex-native-executor-read-only` | 100% |
| `maintenance` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `refactor` | `mutation` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |
| `scout` | `read_only` | `None` | `None` | `None` | `None` | `None` | `None` | N/A |

## 2. Window Variation Analysis

### 30-Day Lookback Window (`2026-08-18T20:43:50+00:00` to `2026-09-17T20:43:50+00:00`)
- **Included Tasks**: 93

- **`bugfix` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required
- **`investigation` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`refactor` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`scout` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### 60-Day Lookback Window (`2026-07-19T20:43:50+00:00` to `2026-09-17T20:43:50+00:00`)
- **Included Tasks**: 263

- **`bugfix` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`feature` (mutation)**: Recommended: `antigravity-native-executor`
- **`feature` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`investigation` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`maintenance` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`refactor` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`scout` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### 90-Day Lookback Window (`2026-06-19T20:43:50+00:00` to `2026-09-17T20:43:50+00:00`)
- **Included Tasks**: 268

- **`bugfix` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`feature` (mutation)**: Recommended: `antigravity-native-executor`
- **`feature` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`investigation` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`maintenance` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`refactor` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`scout` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

## 3. Temporal Split-Half Cohorts

Evaluates temporal stability across non-overlapping partitions of the 90-day window: historical `[as_of-90d, as_of-45d)` and recent `[as_of-45d, as_of]`.

### Cohort: `historical` (`2026-06-19T20:43:50+00:00` to `2026-08-03T20:43:50+00:00`)
- **Included Tasks**: 34

- **`docs` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`feature` (mutation)**: Recommended: _None_
  - Fallback: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor') available; at least 2 required
- **`feature` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`scout` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

### Cohort: `recent` (`2026-08-03T20:43:50+00:00` to `2026-09-17T20:43:50+00:00`)
- **Included Tasks**: 234

- **`bugfix` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`docs` (read_only)**: Recommended: `antigravity-native-executor-read-only`
- **`feature` (mutation)**: Recommended: `antigravity-native-executor`
- **`feature` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`investigation` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`investigation` (read_only)**: Recommended: `codex-native-executor-read-only`
- **`maintenance` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`refactor` (mutation)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples
- **`scout` (read_only)**: Recommended: _None_
  - Fallback: no_eligible_candidates: all candidates lack sufficient samples

## 4. Bootstrap Resampling Sensitivity

Evaluates 10,000 deterministic bootstrap iterations (seed 29) resampling whole task observations independently per profile cell to preserve acceptance and latency correlation. Advisory-only descriptive results; no automated routing threshold is applied.

### Group: `bugfix` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `docs` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `docs` (read_only)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `antigravity-native-executor-read-only` | 28 | 9600 | 0.9600 | R1:9600, R2:400 | R1:0.9600, R2:0.0400 |
| `codex-native-executor-read-only` | 28 | 400 | 0.0400 | R1:400, R2:9600 | R1:0.0400, R2:0.9600 |

### Group: `feature` (mutation)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `antigravity-native-executor` | 74 | 9798 | 0.9798 | R1:9798, R2:202 | R1:0.9798, R2:0.0202 |
| `codex-native-executor` | 25 | 202 | 0.0202 | R1:202, R2:9798 | R1:0.0202, R2:0.9798 |

### Group: `feature` (read_only)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `codex-native-executor-read-only` | 34 | 7489 | 0.7489 | R1:7489, R2:2511 | R1:0.7489, R2:0.2511 |
| `antigravity-native-executor-read-only` | 30 | 2511 | 0.2511 | R1:2511, R2:7489 | R1:0.2511, R2:0.7489 |

### Group: `investigation` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `investigation` (read_only)
- **Status**: `complete`

| Profile | Sample Size | Win Count | Win Prob | Rank Counts (1..M) | Rank Probs (1..M) |
|---|---|---|---|---|---|
| `codex-native-executor-read-only` | 15 | 10000 | 1.0000 | R1:10000, R2:0 | R1:1.0000, R2:0.0000 |
| `antigravity-native-executor-read-only` | 12 | 0 | 0.0000 | R1:0, R2:10000 | R1:0.0000, R2:1.0000 |

### Group: `maintenance` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `refactor` (mutation)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

### Group: `scout` (read_only)
- **Status**: `insufficient_data`
- **Fallback Reason**: insufficient_eligible_candidates: 0 eligible profile(s) meet the sample floor (10); at least 2 required

## 5. Evidence Accounting & 90-Day Snapshot Exclusions

Accounting metrics reflect the full 90-day observation snapshot scanned from the database.

- **Total Tasks Scanned**: 370
- **Included In 90-Day Evidence**: 268
- **Excluded Tasks**: 102

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |

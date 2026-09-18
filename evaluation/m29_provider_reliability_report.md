# M29 Provider Reliability Advisory Report

- **Status**: `partial`
- **Generated At**: `2026-09-17T20:44:45.472395+00:00`
- **Evidence Window**: `2026-06-19T20:43:50+00:00` to `2026-09-17T20:43:50+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 38 | yes |
| `codex-native-executor-read-only` | `read_only` | yes | 77 | yes |
| `antigravity-native-executor` | `mutation` | yes | 83 | yes |
| `antigravity-native-executor-read-only` | `read_only` | yes | 70 | yes |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 370
- **Included In Evidence**: 268
- **Excluded Tasks**: 102

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |

## 2. Recommendations by Task Class and Mode

### Task Class: `bugfix` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (1) | 0.0945 | 46.1s | 12.1d ago | insufficient_sample_size: 2 tasks (minimum 10) |
| - | `codex-native-executor` | no | 5 (4) | 0.3755 | 395.4s | 12.1d ago | insufficient_sample_size: 5 tasks (minimum 10) |

### Task Class: `docs` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (2) | 0.3424 | 70.4s | 43.1d ago | insufficient_sample_size: 2 tasks (minimum 10) |
| - | `codex-native-executor` | no | 3 (3) | 0.4385 | 266.3s | 43.1d ago | insufficient_sample_size: 3 tasks (minimum 10) |

### Task Class: `docs` (read_only)

- **Recommended Profile**: `antigravity-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor-read-only` | yes | 28 (14) | 0.3263 | 45.0s | 0.0d ago | eligible |
| 2 | `codex-native-executor-read-only` | yes | 28 (8) | 0.1525 | 21.7s | 0.0d ago | eligible |

### Task Class: `feature` (mutation)

- **Recommended Profile**: `antigravity-native-executor`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor` | yes | 74 (58) | 0.6773 | 43.9s | 2.0d ago | eligible |
| 2 | `codex-native-executor` | yes | 25 (16) | 0.4452 | 102.8s | 2.2d ago | eligible |

### Task Class: `feature` (read_only)

- **Recommended Profile**: `codex-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `codex-native-executor-read-only` | yes | 34 (24) | 0.5383 | 153.9s | 12.2d ago | eligible |
| 2 | `antigravity-native-executor-read-only` | yes | 30 (19) | 0.4551 | 38.1s | 0.0d ago | eligible |

### Task Class: `investigation` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |
| - | `codex-native-executor` | no | 4 (3) | 0.3006 | 340.8s | 12.3d ago | insufficient_sample_size: 4 tasks (minimum 10) |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: `codex-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `codex-native-executor-read-only` | yes | 15 (15) | 0.7961 | 136.9s | 12.2d ago | eligible |
| 2 | `antigravity-native-executor-read-only` | yes | 12 (9) | 0.4677 | 78.8s | 0.1d ago | eligible |

### Task Class: `maintenance` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 5 (5) | 0.5655 | 73.5s | 43.0d ago | insufficient_sample_size: 5 tasks (minimum 10) |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

### Task Class: `refactor` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |
| - | `codex-native-executor` | no | 1 (0) | 0.0000 | 847.0s | 12.2d ago | insufficient_sample_size: 1 tasks (minimum 10) |

### Task Class: `scout` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |
| - | `codex-native-executor-read-only` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

## 3. Evidence Cells Summary

| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | Repairs | Interventions | Overrides | Med Latency | Budget Cov |
|---|---|---|---|---|---|---|---|---|---|---|
| `bugfix` | `antigravity-native-executor` | `mutation` | 2 | 0.50 [0.09, 0.91] | worker_failure:1 | 0 (0.00) | 1 (0.50) | 1 | 46.1s | 1.00 |
| `bugfix` | `codex-native-executor` | `mutation` | 5 | 0.80 [0.38, 0.96] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 5 | 395.4s | 1.00 |
| `docs` | `antigravity-native-executor` | `mutation` | 2 | 1.00 [0.34, 1.00] | none | 0 (0.00) | 0 (0.00) | 1 | 70.4s | 1.00 |
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 28 | 0.50 [0.33, 0.67] | task_error:9, unknown:2, worker_failure:3 | 2 (0.07) | 4 (0.14) | 28 | 45.0s | 0.68 |
| `docs` | `codex-native-executor` | `mutation` | 3 | 1.00 [0.44, 1.00] | none | 0 (0.00) | 0 (0.00) | 3 | 266.3s | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 28 | 0.29 [0.15, 0.47] | scope_mismatch:1, task_error:8, test_regression:1, unknown:1, worker_failure:9 | 8 (0.29) | 2 (0.07) | 28 | 21.7s | 0.71 |
| `feature` | `antigravity-native-executor` | `mutation` | 74 | 0.78 [0.68, 0.86] | test_regression:4, worker_failure:12 | 6 (0.08) | 4 (0.05) | 74 | 43.9s | 1.00 |
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 30 | 0.63 [0.46, 0.78] | task_error:10, worker_failure:1 | 1 (0.03) | 0 (0.00) | 29 | 38.1s | 0.67 |
| `feature` | `codex-native-executor` | `mutation` | 25 | 0.64 [0.45, 0.80] | test_regression:1, unknown:2, worker_failure:6 | 4 (0.16) | 2 (0.08) | 25 | 102.8s | 0.96 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 34 | 0.71 [0.54, 0.83] | scope_mismatch:2, task_error:1, worker_failure:7 | 1 (0.03) | 0 (0.00) | 34 | 153.9s | 0.97 |
| `investigation` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 12 | 0.75 [0.47, 0.91] | task_error:2, worker_failure:1 | 0 (0.00) | 0 (0.00) | 12 | 78.8s | 0.83 |
| `investigation` | `codex-native-executor` | `mutation` | 4 | 0.75 [0.30, 0.95] | unknown:1 | 0 (0.00) | 0 (0.00) | 4 | 340.8s | 0.75 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 15 | 1.00 [0.80, 1.00] | none | 0 (0.00) | 0 (0.00) | 15 | 136.9s | 1.00 |
| `maintenance` | `antigravity-native-executor` | `mutation` | 5 | 1.00 [0.57, 1.00] | none | 0 (0.00) | 0 (0.00) | 5 | 73.5s | 1.00 |
| `maintenance` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `codex-native-executor` | `mutation` | 1 | 0.00 [0.00, 0.79] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 1 | 847.0s | 1.00 |
| `scout` | `antigravity-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `scout` | `codex-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |

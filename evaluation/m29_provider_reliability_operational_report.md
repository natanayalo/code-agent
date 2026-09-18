# M29 Provider Reliability Advisory Report

- **Status**: `diagnostic_only`
- **Evidence Scope**: `operational`
- **Generated At**: `2026-09-18T21:38:53.381299+00:00`
- **Evidence Window**: `2026-06-20T21:08:09+00:00` to `2026-09-18T21:08:09+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 38 | no |
| `codex-native-executor-read-only` | `read_only` | yes | 87 | no |
| `antigravity-native-executor` | `mutation` | yes | 83 | no |
| `antigravity-native-executor-read-only` | `read_only` | yes | 80 | no |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 392
- **Included In Evidence**: 288
- **Excluded Tasks**: 104

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 2 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |

## 2. Historical Aggregates by Task Class and Mode (Diagnostic-Only)

> [!NOTE]
> Operational scope aggregates evidence across heterogeneous model vintages (e.g. gpt-5.4-mini, gpt-5.6-luna, legacy and current Antigravity) for diagnostic accounting. Recommendations and ranking eligibility are strictly suppressed.

### Task Class: `bugfix` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (1) | 0.0945 | 46.1s | 13.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 5 (4) | 0.3755 | 395.4s | 13.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (2) | 0.3424 | 70.4s | 44.1d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 3 (3) | 0.4385 | 266.3s | 44.1d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 38 (23) | 0.4472 | 96.2s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 38 (18) | 0.3248 | 100.1s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 74 (58) | 0.6773 | 43.9s | 3.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 25 (16) | 0.4452 | 102.8s | 3.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 30 (19) | 0.4551 | 38.1s | 1.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 34 (24) | 0.5383 | 153.9s | 13.3d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 4 (3) | 0.3006 | 340.8s | 13.3d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 12 (9) | 0.4677 | 78.8s | 1.1d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 15 (15) | 0.7961 | 136.9s | 13.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `maintenance` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 5 (5) | 0.5655 | 73.5s | 44.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `refactor` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 1 (0) | 0.0000 | 847.0s | 13.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

## 3. Evidence Cells Summary

| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | Repairs | Interventions | Overrides | Med Latency | Budget Cov |
|---|---|---|---|---|---|---|---|---|---|---|
| `bugfix` | `antigravity-native-executor` | `mutation` | 2 | 0.50 [0.09, 0.91] | worker_failure:1 | 0 (0.00) | 1 (0.50) | 1 | 46.1s | 1.00 |
| `bugfix` | `codex-native-executor` | `mutation` | 5 | 0.80 [0.38, 0.96] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 5 | 395.4s | 1.00 |
| `docs` | `antigravity-native-executor` | `mutation` | 2 | 1.00 [0.34, 1.00] | none | 0 (0.00) | 0 (0.00) | 1 | 70.4s | 1.00 |
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 38 | 0.61 [0.45, 0.74] | infra_verifier_unavailable:3, task_error:9, worker_failure:3 | 2 (0.05) | 4 (0.11) | 38 | 96.2s | 0.76 |
| `docs` | `codex-native-executor` | `mutation` | 3 | 1.00 [0.44, 1.00] | none | 0 (0.00) | 0 (0.00) | 3 | 266.3s | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 38 | 0.47 [0.32, 0.63] | infra_verifier_unavailable:1, scope_mismatch:1, task_error:8, test_regression:1, worker_failure:9 | 10 (0.26) | 2 (0.05) | 38 | 100.1s | 0.79 |
| `feature` | `antigravity-native-executor` | `mutation` | 74 | 0.78 [0.68, 0.86] | test_regression:4, worker_failure:12 | 6 (0.08) | 4 (0.05) | 74 | 43.9s | 1.00 |
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 30 | 0.63 [0.46, 0.78] | task_error:10, worker_failure:1 | 1 (0.03) | 0 (0.00) | 29 | 38.1s | 0.67 |
| `feature` | `codex-native-executor` | `mutation` | 25 | 0.64 [0.45, 0.80] | infra_verifier_unavailable:1, test_regression:1, unknown:1, worker_failure:6 | 4 (0.16) | 2 (0.08) | 25 | 102.8s | 0.96 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 34 | 0.71 [0.54, 0.83] | scope_mismatch:2, task_error:1, worker_failure:7 | 1 (0.03) | 0 (0.00) | 34 | 153.9s | 0.97 |
| `investigation` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 12 | 0.75 [0.47, 0.91] | task_error:2, worker_failure:1 | 0 (0.00) | 0 (0.00) | 12 | 78.8s | 0.83 |
| `investigation` | `codex-native-executor` | `mutation` | 4 | 0.75 [0.30, 0.95] | unknown:1 | 0 (0.00) | 0 (0.00) | 4 | 340.8s | 0.75 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 15 | 1.00 [0.80, 1.00] | none | 0 (0.00) | 0 (0.00) | 15 | 136.9s | 1.00 |
| `maintenance` | `antigravity-native-executor` | `mutation` | 5 | 1.00 [0.57, 1.00] | none | 0 (0.00) | 0 (0.00) | 5 | 73.5s | 1.00 |
| `maintenance` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `codex-native-executor` | `mutation` | 1 | 0.00 [0.00, 0.79] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 1 | 847.0s | 1.00 |

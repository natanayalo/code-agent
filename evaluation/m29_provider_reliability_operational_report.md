# M29 Provider Reliability Advisory Report

- **Status**: `diagnostic_only`
- **Evidence Scope**: `operational`
- **Generated At**: `2026-09-19T21:33:44.374317+00:00`
- **Evidence Window**: `2026-06-21T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 38 | no |
| `codex-native-executor-read-only` | `read_only` | yes | 107 | no |
| `antigravity-native-executor` | `mutation` | yes | 83 | no |
| `antigravity-native-executor-read-only` | `read_only` | yes | 100 | no |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 434
- **Included In Evidence**: 328
- **Excluded Tasks**: 106

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 4 |
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
| - | `antigravity-native-executor` | no | 2 (1) | 0.0945 | 46.1s | 14.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 5 (4) | 0.3755 | 395.4s | 14.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (2) | 0.3424 | 70.4s | 45.1d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 3 (3) | 0.4385 | 266.3s | 45.1d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 38 (23) | 0.4472 | 96.2s | 1.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 38 (18) | 0.3248 | 100.1s | 1.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 74 (58) | 0.6773 | 43.9s | 4.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 25 (16) | 0.4452 | 102.8s | 4.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 40 (24) | 0.4460 | 38.1s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 44 (29) | 0.5114 | 199.6s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 4 (3) | 0.3006 | 340.8s | 14.3d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 22 (14) | 0.4295 | 81.4s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 25 (18) | 0.5242 | 261.6s | 0.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `maintenance` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 5 (5) | 0.5655 | 73.5s | 45.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `refactor` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 1 (0) | 0.0000 | 847.0s | 14.2d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

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
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 40 | 0.60 [0.45, 0.74] | task_error:10, worker_failure:6 | 6 (0.15) | 0 (0.00) | 39 | 38.1s | 0.75 |
| `feature` | `codex-native-executor` | `mutation` | 25 | 0.64 [0.45, 0.80] | infra_verifier_unavailable:1, test_regression:1, unknown:1, worker_failure:6 | 4 (0.16) | 2 (0.08) | 25 | 102.8s | 0.96 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 44 | 0.66 [0.51, 0.78] | infra_verifier_unavailable:5, scope_mismatch:2, task_error:1, worker_failure:7 | 1 (0.02) | 0 (0.00) | 44 | 199.6s | 0.98 |
| `investigation` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 22 | 0.64 [0.43, 0.80] | task_error:2, worker_failure:6 | 6 (0.27) | 0 (0.00) | 22 | 81.4s | 0.91 |
| `investigation` | `codex-native-executor` | `mutation` | 4 | 0.75 [0.30, 0.95] | unknown:1 | 0 (0.00) | 0 (0.00) | 4 | 340.8s | 0.75 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 25 | 0.72 [0.52, 0.86] | infra_verifier_unavailable:5, worker_failure:2 | 0 (0.00) | 0 (0.00) | 25 | 261.6s | 1.00 |
| `maintenance` | `antigravity-native-executor` | `mutation` | 5 | 1.00 [0.57, 1.00] | none | 0 (0.00) | 0 (0.00) | 5 | 73.5s | 1.00 |
| `maintenance` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `refactor` | `codex-native-executor` | `mutation` | 1 | 0.00 [0.00, 0.79] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 1 | 847.0s | 1.00 |

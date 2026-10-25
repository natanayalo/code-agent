# M29 Provider Reliability Advisory Report

- **Status**: `diagnostic_only`
- **Evidence Scope**: `operational`
- **Generated At**: `2026-10-25T10:47:57.841283+00:00`
- **Evidence Window**: `2026-07-28T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

Acceptance is terminal task completion. Verification pass rate is a separate strict stage metric; a completed task can carry a verification warning and still be accepted. Recommendation latency tie-breaks use successful-task latency, while terminal latency remains an operational end-to-end metric.

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 68 | no |
| `codex-native-executor-read-only` | `read_only` | yes | 116 | no |
| `antigravity-native-executor` | `mutation` | yes | 94 | no |
| `antigravity-native-executor-read-only` | `read_only` | yes | 108 | no |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 523
- **Included In Evidence**: 386
- **Excluded Tasks**: 137

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 10 |
| `malformed_inconsistent_timeline` | 1 |
| `non_native_agent_mode` | 2 |
| `outside_window` | 115 |

## 2. Historical Aggregates by Task Class and Mode (Diagnostic-Only)

> [!NOTE]
> Operational scope aggregates evidence across heterogeneous model vintages (e.g. gpt-5.4-mini, gpt-5.6-luna, legacy and current Antigravity) for diagnostic accounting. Recommendations and ranking eligibility are strictly suppressed.

### Task Class: `bugfix` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 2 (1) | 0.0945 | 46.1s | 87.3s | 5.0s | 50.7d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 5 (4) | 0.3755 | 395.4s | 436.6s | 351.0s | 50.7d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 1 (1) | 0.2065 | 78.6s | 78.6s | N/A | 81.6d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 3 (3) | 0.4385 | 266.3s | 266.3s | N/A | 81.6d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `docs` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 33 (19) | 0.4081 | 101.3s | 145.3s | 4.9s | 37.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 34 (14) | 0.2637 | 67.8s | 187.1s | 20.6s | 37.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 86 (68) | 0.6932 | 67.9s | 82.0s | 3.2s | 1.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 55 (45) | 0.6967 | 28.9s | 28.9s | 24.9s | 1.0d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `feature` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 40 (24) | 0.4460 | 38.1s | 160.7s | 5.0s | 36.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 44 (29) | 0.5114 | 199.6s | 178.6s | 281.6s | 36.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 4 (3) | 0.3006 | 340.8s | 244.1s | 534.4s | 50.8d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 35 (24) | 0.5202 | 245.5s | 408.8s | 35.4s | 28.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor-read-only` | no | 38 (24) | 0.4728 | 687.1s | 231.0s | 840.2s | 28.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `maintenance` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 5 (5) | 0.5655 | 73.5s | 73.5s | N/A | 81.5d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

### Task Class: `refactor` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: diagnostic_scope: operational scope aggregates heterogeneous model vintages; recommendations are valid only in current_execution_cohort

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |
| - | `codex-native-executor` | no | 1 (0) | 0.0000 | 847.0s | N/A | 847.0s | 50.7d ago | diagnostic_scope_non_comparable: operational scope aggregates heterogeneous model vintages; recommendations suppressed |

## 3. Evidence Cells Summary

| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | Repairs | Interventions | Overrides | Terminal Med | Success Med | Failure Med | Budget Cov |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bugfix` | `antigravity-native-executor` | `mutation` | 2 | 0.50 [0.09, 0.91] | worker_failure:1 | 0 (0.00) | 1 (0.50) | 1 | 46.1s | 87.3s | 5.0s | 1.00 |
| `bugfix` | `codex-native-executor` | `mutation` | 5 | 0.80 [0.38, 0.96] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 5 | 395.4s | 436.6s | 351.0s | 1.00 |
| `docs` | `antigravity-native-executor` | `mutation` | 1 | 1.00 [0.21, 1.00] | none | 0 (0.00) | 0 (0.00) | 1 | 78.6s | 78.6s | N/A | 1.00 |
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 33 | 0.58 [0.41, 0.73] | infra_verifier_unavailable:3, task_error:8, worker_failure:3 | 2 (0.06) | 2 (0.06) | 33 | 101.3s | 145.3s | 4.9s | 0.76 |
| `docs` | `codex-native-executor` | `mutation` | 3 | 1.00 [0.44, 1.00] | none | 0 (0.00) | 0 (0.00) | 3 | 266.3s | 266.3s | N/A | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 34 | 0.41 [0.26, 0.58] | infra_verifier_unavailable:1, scope_mismatch:1, task_error:8, test_regression:1, worker_failure:9 | 10 (0.29) | 2 (0.06) | 34 | 67.8s | 187.1s | 20.6s | 0.76 |
| `feature` | `antigravity-native-executor` | `mutation` | 86 | 0.79 [0.69, 0.86] | test_regression:4, tool_runtime:3, worker_failure:11 | 6 (0.07) | 2 (0.02) | 86 | 67.9s | 82.0s | 3.2s | 1.00 |
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 40 | 0.60 [0.45, 0.74] | task_error:10, worker_failure:6 | 6 (0.15) | 0 (0.00) | 39 | 38.1s | 160.7s | 5.0s | 0.75 |
| `feature` | `codex-native-executor` | `mutation` | 55 | 0.82 [0.70, 0.90] | infra_verifier_unavailable:1, test_regression:1, unknown:1, worker_failure:7 | 5 (0.09) | 2 (0.04) | 54 | 28.9s | 28.9s | 24.9s | 0.98 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 44 | 0.66 [0.51, 0.78] | infra_verifier_unavailable:5, scope_mismatch:2, task_error:1, worker_failure:7 | 1 (0.02) | 0 (0.00) | 44 | 199.6s | 178.6s | 281.6s | 0.98 |
| `investigation` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 35 | 0.69 [0.52, 0.81] | task_error:2, worker_failure:9 | 8 (0.23) | 0 (0.00) | 35 | 245.5s | 408.8s | 35.4s | 0.94 |
| `investigation` | `codex-native-executor` | `mutation` | 4 | 0.75 [0.30, 0.95] | unknown:1 | 0 (0.00) | 0 (0.00) | 4 | 340.8s | 244.1s | 534.4s | 0.75 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 38 | 0.63 [0.47, 0.77] | infra_verifier_unavailable:6, worker_failure:8 | 0 (0.00) | 0 (0.00) | 38 | 687.1s | 231.0s | 840.2s | 1.00 |
| `maintenance` | `antigravity-native-executor` | `mutation` | 5 | 1.00 [0.57, 1.00] | none | 0 (0.00) | 0 (0.00) | 5 | 73.5s | 73.5s | N/A | 1.00 |
| `maintenance` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `refactor` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `refactor` | `codex-native-executor` | `mutation` | 1 | 0.00 [0.00, 0.79] | worker_failure:1 | 0 (0.00) | 0 (0.00) | 1 | 847.0s | N/A | 847.0s | 1.00 |

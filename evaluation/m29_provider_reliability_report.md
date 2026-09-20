# M29 Provider Reliability Advisory Report

- **Status**: `partial`
- **Evidence Scope**: `current_execution_cohort`
- **Generated At**: `2026-09-19T23:37:24.782095+00:00`
- **Evidence Window**: `2026-06-21T21:32:22.562107+00:00` to `2026-09-19T21:32:22.562107+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

Acceptance is terminal task completion. Verification pass rate is a separate strict stage metric; a completed task can carry a verification warning and still be accepted. Recommendation latency tie-breaks use successful-task latency, while terminal latency remains an operational end-to-end metric.

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | no | 0 | no |
| `codex-native-executor-read-only` | `read_only` | yes | 29 | yes |
| `antigravity-native-executor` | `mutation` | no | 0 | no |
| `antigravity-native-executor-read-only` | `read_only` | yes | 26 | yes |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 434
- **Included In Evidence**: 55
- **Excluded Tasks**: 379

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 4 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |
| `unknown_execution_identity` | 273 |

## 2. Recommendations by Task Class and Mode

### Task Class: `docs` (read_only)

- **Recommended Profile**: `codex-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `codex-native-executor-read-only` | yes | 10 (10) | 0.7225 | 194.6s | 194.6s | N/A | 1.0d ago | eligible |
| 2 | `antigravity-native-executor-read-only` | yes | 10 (9) | 0.5958 | 150.7s | 146.8s | 326.2s | 1.0d ago | eligible |

### Task Class: `feature` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

### Task Class: `feature` (read_only)

- **Recommended Profile**: `antigravity-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor-read-only` | yes | 10 (5) | 0.2366 | 76.7s | 202.9s | 25.2s | 0.0d ago | eligible |
| 2 | `codex-native-executor-read-only` | yes | 10 (5) | 0.2366 | 354.2s | 359.4s | 349.1s | 0.0d ago | eligible |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor-read-only` | no | 6 (5) | 0.4365 | 408.8s | 463.0s | 320.7s | 0.1d ago | insufficient_sample_size: 6 tasks (minimum 10) |
| - | `codex-native-executor-read-only` | no | 9 (3) | 0.1206 | 902.8s | 1086.6s | 831.4s | 0.0d ago | insufficient_sample_size: 9 tasks (minimum 10) |

## 3. Evidence Cells Summary

| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | Repairs | Interventions | Overrides | Terminal Med | Success Med | Failure Med | Budget Cov |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 10 | 0.90 [0.60, 0.98] | infra_verifier_unavailable:1 | 0 (0.00) | 0 (0.00) | 10 | 150.7s | 146.8s | 326.2s | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 10 | 1.00 [0.72, 1.00] | none | 2 (0.20) | 0 (0.00) | 10 | 194.6s | 194.6s | N/A | 1.00 |
| `feature` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 10 | 0.50 [0.24, 0.76] | worker_failure:5 | 5 (0.50) | 0 (0.00) | 10 | 76.7s | 202.9s | 25.2s | 1.00 |
| `feature` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 10 | 0.50 [0.24, 0.76] | infra_verifier_unavailable:5 | 0 (0.00) | 0 (0.00) | 10 | 354.2s | 359.4s | 349.1s | 1.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 6 | 0.83 [0.44, 0.97] | worker_failure:1 | 2 (0.33) | 0 (0.00) | 6 | 408.8s | 463.0s | 320.7s | 1.00 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 9 | 0.33 [0.12, 0.65] | infra_verifier_unavailable:5, worker_failure:1 | 0 (0.00) | 0 (0.00) | 9 | 902.8s | 1086.6s | 831.4s | 1.00 |

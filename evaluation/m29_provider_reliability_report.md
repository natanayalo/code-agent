# M29 Provider Reliability Advisory Report

- **Status**: `partial`
- **Evidence Scope**: `current_execution_cohort`
- **Generated At**: `2026-09-18T21:08:09.989928+00:00`
- **Evidence Window**: `2026-06-20T21:08:09.338278+00:00` to `2026-09-18T21:08:09.338278+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | no | 0 | no |
| `codex-native-executor-read-only` | `read_only` | yes | 10 | yes |
| `antigravity-native-executor` | `mutation` | no | 0 | no |
| `antigravity-native-executor-read-only` | `read_only` | yes | 10 | yes |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 392
- **Included In Evidence**: 20
- **Excluded Tasks**: 372

| Exclusion Reason | Count |
|---|---|
| `cancelled` | 9 |
| `evaluation_smoke` | 2 |
| `malformed_inconsistent_timeline` | 10 |
| `non_native_agent_mode` | 2 |
| `non_temporal_runtime` | 81 |
| `unknown_execution_identity` | 268 |

## 2. Recommendations by Task Class and Mode

### Task Class: `docs` (read_only)

- **Recommended Profile**: `codex-native-executor-read-only`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `codex-native-executor-read-only` | yes | 10 (10) | 0.7225 | 194.6s | 0.0d ago | eligible |
| 2 | `antigravity-native-executor-read-only` | yes | 10 (9) | 0.5958 | 150.7s | 0.0d ago | eligible |

### Task Class: `feature` (mutation)

- **Recommended Profile**: _None_
- **Fallback Reason**: no_eligible_candidates: all candidates lack sufficient samples

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Med Latency | Recency | Notes |
|---|---|---|---|---|---|---|---|
| - | `antigravity-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |
| - | `codex-native-executor` | no | 0 (0) | 0.0000 | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

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
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 10 | 0.90 [0.60, 0.98] | unknown:1 | 0 (0.00) | 0 (0.00) | 10 | 150.7s | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 10 | 1.00 [0.72, 1.00] | none | 2 (0.20) | 0 (0.00) | 10 | 194.6s | 1.00 |
| `feature` | `antigravity-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `feature` | `codex-native-executor` | `mutation` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `scout` | `antigravity-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |
| `scout` | `codex-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | 0.00 |

# M29 Provider Reliability Advisory Report

- **Status**: `partial`
- **Evidence Scope**: `current_execution_cohort`
- **Generated At**: `2026-10-25T10:47:54.881724+00:00`
- **Evidence Window**: `2026-07-28T09:12:04.572097+00:00` to `2026-10-26T09:12:04.572097+00:00` (90 days)
- **Minimum Samples Per Cell**: `10`
- **Confidence Level**: `95%` (Wilson score interval)

Acceptance is terminal task completion. Verification pass rate is a separate strict stage metric; a completed task can carry a verification warning and still be accepted. Recommendation latency tie-breaks use successful-task latency, while terminal latency remains an operational end-to-end metric.

## Enabled Profile Catalog Coverage

| Profile | Mode | Evidence Present | Total Samples | Eligible |
|---|---|---|---|---|
| `codex-native-executor` | `mutation` | yes | 31 | yes |
| `codex-native-executor-read-only` | `read_only` | no | 0 | no |
| `antigravity-native-executor` | `mutation` | yes | 26 | yes |
| `antigravity-native-executor-read-only` | `read_only` | yes | 36 | yes |

## 1. Evidence Accounting & Exclusions

- **Total Tasks Scanned**: 523
- **Included In Evidence**: 93
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

## 2. Recommendations by Task Class and Mode

### Task Class: `docs` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor-read-only` | yes | 10 (9) | 0.5958 | 150.7s | 146.8s | 326.2s | 37.5d ago | eligible |
| - | `codex-native-executor-read-only` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

### Task Class: `feature` (mutation)

- **Recommended Profile**: `codex-native-executor`

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `codex-native-executor` | yes | 31 (30) | 0.8381 | 26.0s | 25.5s | 37.1s | 1.0d ago | eligible |
| 2 | `antigravity-native-executor` | yes | 26 (23) | 0.7102 | 186.6s | 178.7s | 306.2s | 1.0d ago | eligible |

### Task Class: `feature` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor-read-only` | yes | 10 (5) | 0.2366 | 76.7s | 202.9s | 25.2s | 36.5d ago | eligible |
| - | `codex-native-executor-read-only` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

### Task Class: `investigation` (read_only)

- **Recommended Profile**: _None_
- **Fallback Reason**: insufficient_eligible_candidates: only 1 eligible candidate ('antigravity-native-executor-read-only') available; at least 2 required

| Rank | Profile | Eligible | N (Acc) | Wilson 95% Lower | Terminal Med | Success Med | Failure Med | Recency | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `antigravity-native-executor-read-only` | yes | 16 (15) | 0.7167 | 487.4s | 504.2s | 320.7s | 28.5d ago | eligible |
| - | `codex-native-executor-read-only` | no | 0 (0) | 0.0000 | N/A | N/A | N/A | N/A | insufficient_sample_size: 0 tasks (minimum 10) |

## 3. Evidence Cells Summary

| Task Class | Profile | Mode | N | Accepted Rate (95% CI) | Failures | Repairs | Interventions | Overrides | Terminal Med | Success Med | Failure Med | Budget Cov |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `docs` | `antigravity-native-executor-read-only` | `read_only` | 10 | 0.90 [0.60, 0.98] | infra_verifier_unavailable:1 | 0 (0.00) | 0 (0.00) | 10 | 150.7s | 146.8s | 326.2s | 1.00 |
| `docs` | `codex-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `feature` | `antigravity-native-executor` | `mutation` | 26 | 0.88 [0.71, 0.96] | tool_runtime:3 | 0 (0.00) | 0 (0.00) | 26 | 186.6s | 178.7s | 306.2s | 1.00 |
| `feature` | `antigravity-native-executor-read-only` | `read_only` | 10 | 0.50 [0.24, 0.76] | worker_failure:5 | 5 (0.50) | 0 (0.00) | 10 | 76.7s | 202.9s | 25.2s | 1.00 |
| `feature` | `codex-native-executor` | `mutation` | 31 | 0.97 [0.84, 0.99] | worker_failure:1 | 1 (0.03) | 0 (0.00) | 30 | 26.0s | 25.5s | 37.1s | 1.00 |
| `feature` | `codex-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |
| `investigation` | `antigravity-native-executor-read-only` | `read_only` | 16 | 0.94 [0.72, 0.99] | worker_failure:1 | 2 (0.12) | 0 (0.00) | 16 | 487.4s | 504.2s | 320.7s | 1.00 |
| `investigation` | `codex-native-executor-read-only` | `read_only` | 0 | 0.00 [0.00, 0.00] | none | 0 (0.00) | 0 (0.00) | 0 | N/A | N/A | N/A | 0.00 |

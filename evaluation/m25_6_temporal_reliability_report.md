# M25.6 Temporal Reliability Baseline

- Status: `ready_for_operator_review`
- Build SHA: `c2789a36ecaf5e806d556322dd7a86191192f9c2`
- Environment: `m25-6-local`
- Cases: 20/20 captured; 20 valid
- Human interventions: 6
- Repeated clarification questions: 0
- Validation-evidence rate: 1.0
- CI/review rejections: 0/0

| Case | Category | Profile | Evidence | Observed | Valid |
| --- | --- | --- | --- | --- | --- |
| draft-pr-antigravity | draft_pr | antigravity-native-executor | current (c2789a36ecaf) | completed | yes |
| draft-pr-codex | draft_pr | codex-native-executor | reused (bac3ebb56c95) | completed | yes |
| fanout-antigravity-01 | read_only_fanout | antigravity-native-executor-read-only | reused (0aa7f30f802c) | completed | yes |
| fanout-codex-01 | read_only_fanout | codex-native-executor-read-only | reused (0e3081ec3f19) | completed | yes |
| hitl-approval | hitl | codex-native-executor | reused (f3abac7d037d) | completed | yes |
| hitl-clarification | hitl | antigravity-native-executor | reused (f3abac7d037d) | completed | yes |
| hitl-permission-escalation | hitl | antigravity-native-executor | reused (c8a765269249) | completed | yes |
| mutation-antigravity-01 | mutation | antigravity-native-executor | reused (51d23c887bfc) | completed | yes |
| mutation-codex-01 | mutation | codex-native-executor | reused (51d23c887bfc) | completed | yes |
| mutation-independent-review-repair | mutation | antigravity-native-executor | reused (f3abac7d037d) | completed | yes |
| mutation-verifier-repair | mutation | codex-native-executor | reused (d0b403eb66c1) | completed | yes |
| read-only-antigravity-01 | read_only_monolithic | antigravity-native-executor-read-only | reused (51d23c887bfc) | completed | yes |
| read-only-antigravity-02 | read_only_monolithic | antigravity-native-executor-read-only | reused (51d23c887bfc) | completed | yes |
| read-only-codex-01 | read_only_monolithic | codex-native-executor-read-only | reused (51d23c887bfc) | completed | yes |
| read-only-codex-02 | read_only_monolithic | codex-native-executor-read-only | reused (2b0cbeab5a67) | completed | yes |
| recovery-cancellation | recovery | codex-native-executor | reused (bbd146650f08) | cancelled | yes |
| recovery-worker-restart | recovery | antigravity-native-executor | reused (bbd146650f08) | completed | yes |
| sequential-dag-antigravity-01 | sequential_dag | antigravity-native-executor | reused (0e3081ec3f19) | completed | yes |
| sequential-dag-codex-01 | sequential_dag | codex-native-executor | reused (0e3081ec3f19) | completed | yes |
| sequential-dag-codex-02 | sequential_dag | codex-native-executor | reused (0e3081ec3f19) | completed | yes |

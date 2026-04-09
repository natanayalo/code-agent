# Status

Live progress is tracked here.

Use this file for current execution state.
Use `docs/mvp_backlog.md` for the canonical task catalog and scope.

## Done

- Initial repo guidance and architecture docs. PR: [#1](https://github.com/natanayalo/code-agent/pull/1)
- Planning doc refinements and task scaffolding. PR: [#2](https://github.com/natanayalo/code-agent/pull/2)
- T-003 Add health endpoints. PR: [#3](https://github.com/natanayalo/code-agent/pull/3)
- Repo quality hooks and CI. PR: [#4](https://github.com/natanayalo/code-agent/pull/4)
- T-002 Add local infrastructure. PR: [#5](https://github.com/natanayalo/code-agent/pull/5)
- T-010 Add DB models. PR: [#6](https://github.com/natanayalo/code-agent/pull/6)
- T-011 Add repository layer. PR: [#11](https://github.com/natanayalo/code-agent/pull/11)
- T-012 Define orchestrator state schema. PR: [#12](https://github.com/natanayalo/code-agent/pull/12)
- T-020 Build LangGraph workflow skeleton. PR: [#13](https://github.com/natanayalo/code-agent/pull/13)
- T-013 Normalize persistence enums and constrained value fields. PR: [#14](https://github.com/natanayalo/code-agent/pull/14)
- T-021 Add checkpoint persistence. PR: [#15](https://github.com/natanayalo/code-agent/pull/15)
- T-022 Add approval interrupt node. PR: [#16](https://github.com/natanayalo/code-agent/pull/16)
- T-030 Create workspace manager. PR: [#17](https://github.com/natanayalo/code-agent/pull/17)
- T-031 Add Docker sandbox runner. PR: [#18](https://github.com/natanayalo/code-agent/pull/18)
- CI validation hardening. PR: [#19](https://github.com/natanayalo/code-agent/pull/19)
- T-032 Add artifact capture. PR: [#20](https://github.com/natanayalo/code-agent/pull/20)
- Implementation plan critical-path refinement. PR: [#21](https://github.com/natanayalo/code-agent/pull/21)
- T-040 Define worker interface. PR: [#22](https://github.com/natanayalo/code-agent/pull/22)
- T-041 Implement `CodexWorker` through the shared async worker contract. PR: [#23](https://github.com/natanayalo/code-agent/pull/23)
- Reference-analysis plan updates for persistent shell, prompt, and agent-loop sequencing. PR: [#24](https://github.com/natanayalo/code-agent/pull/24)
- Architecture review checkpoint: align orchestrator route and dispatch state under real worker execution. PR: [#25](https://github.com/natanayalo/code-agent/pull/25)
- T-045 Evolve sandbox to persistent container with shell sessions. PR: [#26](https://github.com/natanayalo/code-agent/pull/26)
- T-046 Build structured system prompt module. PR: [#28](https://github.com/natanayalo/code-agent/pull/28)
- T-048 Add an explicit tool registry and policy-aware bash tool boundary. PR: [#32](https://github.com/natanayalo/code-agent/pull/32)
- T-047 Shared CLI-driven multi-turn worker runtime slice (without provider wiring). PR: [#31](https://github.com/natanayalo/code-agent/pull/31)
- T-049 Permission ladder and runtime budget ledger/enforcement slice. PR: [#33](https://github.com/natanayalo/code-agent/pull/33)
- T-042 Baseline worker timeout/cancel envelope slice. PR: [#34](https://github.com/natanayalo/code-agent/pull/34)
- T-044 / T-047 Wire real provider CLI adapter into the app path. PR: [#36](https://github.com/natanayalo/code-agent/pull/36)
- T-049 Wire permission-required outcomes into orchestrator pause/resume. PR: [#37](https://github.com/natanayalo/code-agent/pull/37)
- T-042 Orchestrator Timeout Diagnostics: extract partial execution results and workspace artifacts after worker cancellation. (Local verification complete)
- T-055 Add the constrained verifier stage. PR: [#38](https://github.com/natanayalo/code-agent/pull/38)
- Milestone 6: Sandbox hardening (T-054/T-055). Verified with strict path policies, secret redaction, and complete audit capture.
- Milestone 7: Skeptical memory, compact session state, and stable session scaffold (T-060 to T-065). Verified with schema metadata and SessionState repository.
- Milestone 8: Structured run observability (T-043). PR: [#45](https://github.com/natanayalo/code-agent/pull/45)
- T-070 Implement GeminiCliWorker + GeminiCliRuntimeAdapter as second worker. PR: [#46](https://github.com/natanayalo/code-agent/pull/46)
- T-071 routing heuristics + T-072 manual override. PR: [#47](https://github.com/natanayalo/code-agent/pull/47)
- T-050 Generic webhook adapter. PR: [#48](https://github.com/natanayalo/code-agent/pull/48)
- T-051 Telegram webhook adapter (Milestone 10). PR: [#49](https://github.com/natanayalo/code-agent/pull/49)
- T-052 Progress replies (Milestone 10). PR: [#50](https://github.com/natanayalo/code-agent/pull/50)
- T-053 Dedupe protection for repeated webhook deliveries (Milestone 10). PR: [#50](https://github.com/natanayalo/code-agent/pull/50)
- Milestone 10: Telegram ingress milestone (T-050 to T-053). PR: [#50](https://github.com/natanayalo/code-agent/pull/50)
- T-084 Add lifespan-managed shared HTTP clients for outbound notifier adapters. PR: [#51](https://github.com/natanayalo/code-agent/pull/51)

## In Progress

- T-085 Isolate parallel progress notifier delivery with per-backend timeout/error handling.

## Next
- T-086 Harden outbound callback SSRF defenses beyond literal-IP validation.

## Blocked

- None

## Notes

- Current target order from here: T-085 parallel notifier delivery isolation → T-086 callback SSRF hardening.
- The core execution path handles iterative agent loops (T-047), persistent shell sessions (T-045), and structured system prompts (T-046) using the real `CodexCliWorker` and `codex exec` adapter.
- The vertical slice (T-044) is wired: the app can bootstrap the `TaskExecutionService` and execute multi-turn tasks in a provisioned sandbox workspace.
- Safety layering is intentional: T-047/T-049 carry the inner-loop brakes and permission-aware tool execution; T-042 adds the outer orchestrator-level timeout/cancel layer that preserves workspace artifacts and surfaces diagnostics.
- T-055 (Verifier) performs deterministic checks on worker output, including test results and command audit logs, before final summarization.
- T-054 (Sandbox Hardening) ensures strict path policies, secret redaction, and complete audit artifact capture for all sandbox executions.
- Milestone 7 (Memory Integration) adds skepticism metadata (provenance, confidence) to all memory entries and maintains a compact `SessionState` for cross-task goal and risk tracking.
- The near-term worker plan is explicitly CLI-first. The second worker will be a Gemini CLI adapter. New worker work should not assume full ownership of low-level raw API payload assembly when a CLI, SDK, hook, or subprocess adapter can provide the runtime.
- T-044 DB scope remains intentionally limited to execution-path persistence for task/status lookup, worker run metadata, final result fields, verifier output, and captured artifacts needed for polling by `task_id`.
- The original `CodexWorker` remains in the repo as a sandboxed toy executor for contract/proof-of-path testing, but is superseded by `CodexCliWorker` for real tasks.
- CI validates every push, including merges to `master`, enforcing a 90% branch-coverage floor in `pytest`.
- T-021 adds durable LangGraph checkpointing; T-022 adds a destructive-action approval pause/resume path.
- T-030/T-031/T-032 provide workspace provisioning, Docker-based execution, and artifact capture.
- Review follow-ups identified during PR #50: add lifespan-managed shared HTTP clients for outbound notifier adapters (T-084), isolate parallel progress notifier delivery with per-backend timeout/error handling (T-085), and harden callback SSRF defenses beyond literal-IP validation (T-086).

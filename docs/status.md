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

## In Progress

- None

## Next
- T-054 Harden sandbox execution boundary and auditability.
- Milestone: skeptical memory, compact session state, and stable session scaffold (T-060 to T-065).
- Milestone: structured run observability and second worker routing (T-043, T-070+) after the vertical slice, verifier, and memory loop are stable.
- Milestone: Telegram ingress (T-050 to T-053) after the core execution path is stable.

## Blocked

- None

## Notes

- Current target order from here: T-047 shared CLI worker runtime → T-048 tool registry → T-049 permission ladder + runtime budget enforcement → T-042 outer timeout/cancel → T-044 vertical slice with a real CLI worker, HTTP submit path, and execution-path DB persistence → T-055 verifier stage → T-054 sandbox auditability/hardening → T-060..T-065 skeptical memory, compact session state, and stable scaffold persistence → T-043 structured run observability → T-070+ second worker → Telegram/webhook adapters.
- T-045 and T-046 are now both landed in-repo: the persistent container/shell primitives exist, and the structured system prompt module exists. The remaining gap is the actual CLI-driven worker loop that uses them.
- T-044 / T-047 is now partially wired in-repo: the app can bootstrap the real
  `TaskExecutionService` from env, and `CodexCliWorker` can now delegate planning turns to a
  concrete `codex exec` adapter instead of a test-only scripted adapter. The remaining gap is
  live validation with a configured CLI/auth environment and publishing this slice.
- The near-term worker plan is explicitly CLI-first. New worker work should not assume full ownership of low-level raw API payload assembly when a CLI, SDK, hook, or subprocess adapter can provide the runtime.
- Safety layering is intentional: T-047/T-049 carry the inner-loop brakes and permission-aware tool execution so the worker cannot run unbounded when exercised standalone; T-042 then adds the outer orchestrator-level timeout/cancel layer that preserves workspace/logs and surfaces timeout state without hanging the run forever.
- T-044 DB scope remains intentionally limited to execution-path persistence for task/status lookup, worker run metadata, final result fields, verifier output, and captured artifacts needed for polling by `task_id`.
- The current `CodexWorker` is still a sandboxed toy executor used to prove the worker contract and artifact capture path. It is not yet the target CLI-runtime implementation.
- CI now validates every push, including merges to `master`, avoiding duplicate pull request branch runs while enforcing a 90% branch-coverage floor in `pytest`.
- Protected-branch enforcement for `master` still depends on GitHub branch protection settings and required status checks.
- T-021 adds durable LangGraph checkpointing without yet wiring orchestrator state to the app layer.
- T-022 adds a destructive-action approval pause/resume path before worker dispatch.
- T-030 adds per-task workspace provisioning, repo clone, and cleanup-policy scaffolding.
- T-031 adds Docker-based command execution with mounted workspaces and captured stdout/stderr.
- T-032 writes per-command stdout/stderr logs plus changed-file and diff-summary artifacts into
  each sandbox workspace for later worker-run persistence.
- Architecture checkpoint review found two mismatches under real execution: the graph could claim
  `claude` while only a Codex worker was wired, and `dispatch.run_id` / `dispatch.workspace_id`
  were placeholder values before a real run existed. The current slice makes both cases explicit.
- T-045 landed with a persistent container manager, a long-lived shell session API, shared bounded
  stream helpers, and unit/integration coverage for state persistence across commands while keeping
  the existing one-shot `DockerSandboxRunner` intact.

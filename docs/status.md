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

## In Progress

- None

## Next

- T-046 Build structured system prompt module
- T-047 Implement multi-turn agent loop in worker (ClaudeWorker, bash-only tools, with worker-local timeout/max-iteration/budget guards)
- T-042 Add baseline worker timeout/cancel handling (outer orchestrator-level timeout/cancel envelope around the real worker path)
- T-044 Run one real orchestrator-to-worker vertical slice (multi-step agent loop, not toy script, with execution-path DB persistence only)
- Milestone: Telegram ingress (T-050 to T-053)
- T-054 Enforce sandbox execution boundary and destructive-action approval gate
- Milestone: Memory integration (T-060 to T-064)
- Milestone: structured run observability and second worker routing (T-043, T-070+) after memory integration

## Blocked

- None

## Notes

- Current target order from here: T-046 system prompt → T-047 self-bounded agent loop → T-042 outer timeout/cancel → T-044 vertical slice with multi-turn agent worker, HTTP submit path, and execution-path DB persistence → Telegram ingress/adapters → T-054 sandbox hardening → T-060..T-064 memory integration → T-043 structured run observability → T-070+ second worker.
- T-045/T-046/T-047 were added based on the reference implementation analysis (Open-SWE, Deep Agents, mini-SWE-agent, SWE-ReX, OpenHands, Stripe/Ramp/Coinbase). Key insight: every successful coding agent is built on a persistent interactive shell and a carefully engineered system prompt.
- Safety layering is intentional: T-047 carries the inner-loop brakes (max iterations, worker-local timeout, budget checks) so the worker cannot run unbounded when exercised standalone; T-042 then adds the outer orchestrator-level timeout/cancel layer that preserves workspace/logs and surfaces timeout state without hanging the run forever.
- T-044 DB scope is intentionally limited to execution-path persistence for task/status lookup, worker run metadata, final result fields, and captured artifacts. Wiring `load_memory` / `persist_memory` into structured memory repositories remains part of the later T-060..T-064 memory milestone.
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

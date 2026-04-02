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
- T-013 Normalize persistence enums and constrained value fields. PR: [#14](https://github.com/natanayalo/code-agent/pull/14)

## In Progress

- T-012 Define orchestrator state schema
- T-020 Build LangGraph workflow skeleton
- T-021 Add checkpoint persistence
- T-022 Add approval interrupt node
- T-030 Create workspace manager
- T-031 Add Docker sandbox runner
- T-032 Add artifact capture

## Next

- T-040 Define worker interface

## Blocked

- None

## Notes

- Milestone 1 close-out and Milestone 2 skeleton work are both active.
- CI now validates every push, including merges to `master`, avoiding duplicate pull request branch runs while enforcing a 90% branch-coverage floor in `pytest`.
- Protected-branch enforcement for `master` still depends on GitHub branch protection settings and required status checks.
- T-021 adds durable LangGraph checkpointing without yet wiring orchestrator state to the app layer.
- T-022 adds a destructive-action approval pause/resume path before worker dispatch.
- T-030 adds per-task workspace provisioning, repo clone, and cleanup-policy scaffolding.
- T-031 adds Docker-based command execution with mounted workspaces and captured stdout/stderr.
- T-032 writes per-command stdout/stderr logs plus changed-file and diff-summary artifacts into
  each sandbox workspace for later worker-run persistence.

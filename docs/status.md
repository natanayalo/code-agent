# Status

## Current Phase

Phase 3: personal reliability before broader autonomy.

Active focus:

- Phase 3 roadmap and M20.0/M20.1 reliability planning
- Milestone 21 (Worker Runtime Hotspot Refactor) boundary extraction after M20 contracts settle

## Current Capabilities

- API + Telegram ingress for task intake
- shared-secret API auth for protected ingress routes
- durable Postgres persistence for users/sessions/tasks/runs/artifacts/memory
- split API/worker runtime with queue polling and lease claims
- LangGraph orchestrator with worker routing, approval checkpoints, verifier stage, and timeline persistence
- worker adapters for Codex CLI, Antigravity CLI, and OpenRouter-backed execution
- sandboxed workspace/container execution with command artifact capture and retention controls
- skeptical memory + compact session state persistence
- operational controls: task replay, approval decision endpoint, progress callbacks, and metrics
- generated TaskSpec contract for task goal/risk/type/delivery policy before worker routing
- PR-native delivery fields with GitHub branch/draft-PR delivery integration
- dashboard visibility for TaskSpec, interactions, timeline events, logs, artifacts, replay controls, traces, memory, and tool inventory
- CI now measures Python coverage from `tests/unit` only and runs `tests/integration` as a separate pass
- pre-commit Ruff checks repo Python files for non-top-level imports while preserving a few intentional lazy imports in guarded modules
- shipped changes are tracked in [`CHANGELOG.md`](../CHANGELOG.md)

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- Codex/Antigravity now support native-agent defaults behind rollback flags, but deeper verifier/repair integration is still in progress
- Antigravity non-interactive runs use prompt-as-argv and permission/settings policy, so command logging and profile mapping need explicit redaction and tests
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. finish M20.1 Runtime Operating Contract
2. implement M20.2 ExecutionPlan Spine as an observable scaffold
3. defer Milestone 21 Hotspot Refactor until M20 worker/operator contracts stabilize
4. keep Phase 4 autonomy work gated on Phase 3 reliability metrics

## Current Backlog

Granular tasks for the active and upcoming milestones:

- M20.1: persist the `RuntimeManifest` generated during execution alongside the `WorkerRun` entity.
- M20.2: add durable ExecutionPlan/ExecutionPlanNode spine without replacing task scheduling or enabling fan-out.
- M20.3: upgrade HumanInteraction payloads into decision cards, HITL modes, and interaction-based inbox rows.
- M20.4: add worker registry, heartbeat, capacity, health, and quarantine primitives for safe routing/backpressure.
- M20.5: add repo validation profiles and require validation evidence or bounded failure reports.
- M20.6: persist GitHub draft PR/CI metadata and create focused repair tasks for failed checks.
- M20.7: expand the eval suite and compare reliability/profile/stage metrics against the M20.0 baseline.
- Phase 4: evaluate routing, semantic memory, DAG decomposition, selective fan-out, review repair, and autonomy policy only after Phase 3 proves reliability.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

# Status

## Current Phase

Phase 2: bounded autonomy.

Active focus:

- Milestone 19 (Reflection and Improvement Pipeline) execution
- Milestone 19.5 (Gemini to Antigravity Migration) planning and kickoff
- dashboard QA and operator polish for reflection/scout workflows

## Current Capabilities

- API + Telegram ingress for task intake
- shared-secret API auth for protected ingress routes
- durable Postgres persistence for users/sessions/tasks/runs/artifacts/memory
- split API/worker runtime with queue polling and lease claims
- LangGraph orchestrator with worker routing, approval checkpoints, verifier stage, and timeline persistence
- worker adapters for Codex CLI, Gemini CLI, and OpenRouter-backed execution
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
- Codex/Gemini now support native-agent defaults behind rollback flags, but deeper verifier/repair integration is still in progress
- Gemini CLI personal OAuth access is no longer a reliable local/e2e default; Antigravity migration must preserve worker identity, auth, Docker, and e2e safety
- Antigravity CLI auth is keyring-backed, and Linux Docker workers may need official Secret Service/DBus support before e2e can pass safely
- Antigravity non-interactive runs use prompt-as-argv and permission/settings policy, so command logging and profile mapping need explicit redaction and tests
- native-agent runs may initially have coarser command-level audit unless CLI event streams are captured and normalized
- OpenRouter remains useful for eval/raw-chat experiments but should be isolated as legacy tool-loop mode during the migration
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. full dashboard QA, visual polish, limits, and reusable QA skill
2. execute Milestone 19.5 Gemini to Antigravity migration planning tasks
3. continue tightening native-agent observability and verifier acceptance policy

Reference baseline:

- official Antigravity CLI manuals are linked from [Milestone 19.5](roadmap.md#milestone-195-gemini-to-antigravity-migration) and should be rechecked before implementing T-205 through T-211

## Current Backlog

Granular tasks for the active and upcoming milestones:

- T-199: Full dashboard QA, visual polish, limits, and reusable QA skill.
- T-205: Rename canonical worker identity from Gemini to Antigravity.
- T-206: Add Antigravity native CLI adapter with `agy -p` / `--print`, settings generation, and permission handling.
- T-207: Extend native runner to support prompt-as-argv for `agy` with command-log redaction.
- T-208: Add Docker Antigravity support using official install/auth/keyring mechanisms.
- T-209: Update e2e QA scripts, README, runbook, `.env.example`, compose docs, and operator guidance for install, keyring/DBus auth, permissions, context files, skills, and MCP migration.
- T-210: Update dashboard/API types and labels for `antigravity` worker/profile names.
- T-211: Remove Gemini CLI defaults after Antigravity Docker e2e passes.
- T-200: Skip change-oriented review for read-only tasks.
- T-201: Add structured Scout trigger parameters and repo allowlist.
- T-202: Add task-type prompt overlays for Scout modes.
- T-203: Implement Repo Scout and Research Scout modes.
- T-204: Implement Deep Scout chaining.

## Completed Work

Completed work is tracked in [`CHANGELOG.md`](../CHANGELOG.md). Keep this file
focused on the current phase, active risks, and upcoming priorities.

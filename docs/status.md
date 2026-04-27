# Status

## Current Phase

Phase 1: clarity and control.

Active focus:

- Milestone A (TaskSpec foundation and human workflow)
- Milestone 16 (operator dashboard/PWA)
- preparation for Milestone 17 (worker mode/runtime profile strategy)

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

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- TaskSpec can surface clarification/permission needs, but generic HumanInteraction states are not first-class yet
- PR-native delivery is represented as desired delivery metadata only; branch/PR creation is still a future slice
- runtime profile strategy (planner/executor/reviewer/scout defaults) is not yet fully codified
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. complete TaskSpec + HumanInteraction foundation before expanding worker autonomy
2. implement Milestone 16 detailed dashboard views around TaskSpec, timeline, logs, artifacts, replay controls
3. implement Milestone 17 worker profile + capability matrix + policy/config mapping
4. add PR-native delivery fields and GitHub branch/draft-PR integration
5. introduce evals before adding more scout/autonomy behavior

## Current Backlog

Granular tasks for the active and upcoming milestones:

### Milestone A: TaskSpec and Human Workflow Foundation
- [x] T-146: add TaskSpec model, persistence, deterministic generation, API visibility, and focused tests
- [x] T-147: add HumanInteraction model for clarification, permission, review, merge, and blocked-help states (#134)
- [ ] T-148: map TaskSpec clarification/permission flags into resumable HumanInteraction records
- [ ] T-149: show TaskSpec and pending interactions in dashboard task detail/operator inbox

### Milestone 16: Operator UX (Dashboard/PWA)
- [x] T-130: design PWA frontend architecture and choose tech stack (React/Vite) (#114)
- [x] T-131: implement API endpoints for task/session listing and detailed view
- [x] T-132: build core dashboard layout with task status board
- [x] T-133: implement comprehensive test suite and CI (90% coverage)
- [x] T-134: implement approval/rejection UI components in the dashboard
- [x] T-135: implement task replay control (unchanged) in the dashboard (#115)
- [x] T-136: implement secure dashboard authentication (HttpOnly cookies/OIDC)
- [x] T-137: implement dashboard routing and pages for Sessions/Metrics (backend support exists)
- [ ] T-138: implement detailed task view (timeline, logs, artifacts)
- [ ] T-139: implement "Replay with Overrides" modal/form in the dashboard
- [ ] T-143: implement API & UI for Session Working Context (Goal/Risks/Decisions)
- [ ] T-144: implement API & UI for Knowledge Base (Skeptical Memory) management
- [ ] T-145: implement API & UI for Tool Inventory and Sandbox status

### Milestone 17: Worker Profile Strategy
- [ ] T-140: define `WorkerProfile` Pydantic model and capability matrix
- [ ] T-141: implement profile-based worker selection logic in the orchestrator
- [ ] T-142: map existing workers (Gemini, Codex, OpenRouter) to capabilities (Planning, Coding, Reviewing)

## Recent Completed Milestones

- Milestone 10: Telegram ingress and progress update flow (T-050 to T-053)
- Milestone 11: tool wrappers and MCP compatibility slices (T-080 to T-089, T-107)
- Milestone 12: observability + replay (T-090 to T-092)
- Milestone 13 (remainder): hardening controls including auth/safety/budget/retention (T-100 to T-105)
- Milestone 14 baseline: planning/context/review intelligence slices (T-106, T-108 to T-112, T-114 to T-128)
- Milestone 15: product identity and documentation refresh

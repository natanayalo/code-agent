# Status

## Current Phase

Phase 1: clarity and control.

Active focus:

- Milestone A (product identity + docs refresh)
- preparation for Milestone B (operator dashboard/PWA)
- preparation for Milestone C (worker mode/runtime profile strategy)

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

## Open Risks

- operator inspection/control still relies on API + logs more than dedicated UI
- runtime profile strategy (planner/executor/reviewer/scout defaults) is not yet fully codified
- autonomy/reflection work is not yet separated into a bounded scout lane
- worker runtime internals still contain hotspot complexity despite recent decomposition progress

## Next Priorities

1. complete Milestone A documentation refresh and keep docs synchronized with behavior
2. implement Milestone B thin local dashboard/PWA for visibility, approvals, replay/retry controls
3. implement Milestone C worker profile + capability matrix + policy/config mapping
4. define bounded Scout mode design (Milestone D) with explicit budget and permission boundaries
5. introduce structured friction/improvement proposal pipeline (Milestone E)

## Current Backlog

Granular tasks for the active and upcoming milestones:

### Milestone B: Operator UX (Dashboard/PWA)
- [x] T-130: design PWA frontend architecture and choose tech stack (React/Vite) (#114)
- [ ] T-131: implement API endpoints for task/session listing and detailed view
- [ ] T-132: build core dashboard layout with task status board
- [ ] T-133: implement approval/rejection UI components in the dashboard
- [ ] T-134: implement task replay/retry controls in the dashboard

### Milestone C: Worker Profile Strategy
- [ ] T-140: define `WorkerProfile` Pydantic model and capability matrix
- [ ] T-141: implement profile-based worker selection logic in the orchestrator
- [ ] T-142: map existing workers (Gemini, Codex, OpenRouter) to capabilities (Planning, Coding, Reviewing)

## Recent Completed Milestones

- Milestone 10: Telegram ingress and progress update flow (T-050 to T-053)
- Milestone 11: tool wrappers and MCP compatibility slices (T-080 to T-089, T-107)
- Milestone 12: observability + replay (T-090 to T-092)
- Milestone 13 (remainder): hardening controls including auth/safety/budget/retention (T-100 to T-105)
- Milestone 14 baseline: planning/context/review intelligence slices (T-106, T-108 to T-112, T-114 to T-128)

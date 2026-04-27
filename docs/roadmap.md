# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Gemini/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 1: clarity and control.

Priority sequence:

1. Milestone 15: Product identity and documentation refresh
2. Milestone A: TaskSpec and human workflow foundation
3. Milestone 16: Operator UX and transparency
4. Milestone 17: Worker profile and runtime leverage

Planned next phases:

1. Phase 2: bounded autonomy
2. Phase 3: deeper platform maturity

## Milestone 15: Product Identity and Documentation Refresh

Goal:

- align public and internal docs with current platform behavior and near-term direction

Deliverables:

- rewritten `README.md`
- refreshed `docs/architecture.md`
- new `docs/runbook.md`
- new `docs/roadmap.md`
- concise `docs/status.md`

Status:

- completed (documentation synchronized with behavior)

## Milestone A: TaskSpec and Human Workflow Foundation

Goal:

- establish a structured task contract and first-class human checkpoints before expanding worker autonomy

Planned deliverables:

- persisted `TaskSpec` with goal, task type, risk, acceptance criteria, policy, verification, and delivery metadata
- deterministic TaskSpec generation before worker routing
- task status API and dashboard visibility for TaskSpec
- generic HumanInteraction records for clarification, permission, final review, merge approval, and blocked help
- compatibility path from existing approval endpoints to HumanInteraction

Non-goals:

- branch/PR creation
- merge automation
- scout/autonomy expansion
- replacing worker-profile routing in this milestone

Success criteria:

- every worker run starts from an inspectable task contract
- ambiguous and high-risk tasks expose the precise human checkpoint needed
- dashboard/operator views can prioritize tasks needing input

## Milestone 16: Operator UX and Transparency

Goal:

- provide richer task inspection and intervention than Telegram-only flows

Planned deliverables:

- thin local dashboard/PWA
- task list + task detail views
- runtime stage, active worker, and status visualization
- command timeline, changed files, and artifact visibility
- approval inbox and explicit operator controls
- retry/replay/cancel actions from UI
- push-style notifications for approval needed, completion, failure, and ideas

Non-goals:

- native iOS/Android app
- replacing chat as intent interface
- broad multi-user admin console

Success criteria:

- active task inspection without direct log reading
- faster/clearer approval flow than Telegram-only
- replay/retry invokable from dashboard

## Milestone 17: Worker Mode and Runtime Leverage

Goal:

- reduce duplicated platform logic by using strong runtime-native capabilities where appropriate

Planned deliverables:

- worker profile definitions: planner, executor, reviewer, router/control, scout
- runtime capability matrix (planning, subagents, autonomy modes, skills/hooks, structured output)
- worker selection policy docs + config-driven defaults
- explicit buy-vs-build decisions for at least one major capability

Default direction (subject to validation):

- planner: Gemini profile
- primary executor: Codex profile
- specialized/richer coding mode: OpenRouter-backed profile where beneficial
- reviewer: stronger reviewer profile
- control-plane chores: cheaper/smaller profile

Non-goals:

- implementing every runtime feature
- forcing one runtime to handle all roles

## Milestone 18: Controlled Autonomy / Scout Mode

Goal:

- add bounded proactive exploration without destabilizing primary execution

Planned deliverables:

- separate scout mode lane, queue, and budget policy
- read-mostly default permissions
- idea inbox/proposal store
- trigger sources: schedule, idle time, manual prompts, recurring failure signals

Required controls:

- explicit budget cap
- no direct production mutation
- output routed to review inbox only

## Milestone 19: Reflection and Improvement Pipeline

Goal:

- convert execution friction into structured, reviewable improvement proposals

Planned deliverables:

- friction report schema
- improvement suggestion schema
- proposal scoring/planning by value, effort, risk, layer impact, validation path, and HITL need
- review queue for improvement proposals

Manual-only zones:

- auth/security
- secrets/sandbox boundaries
- approval core logic
- deployment/billing controls

## Milestone 20: Operational Self-Awareness

Goal:

- make runtime identity, constraints, and maintenance paths explicit to workers/operators

Planned deliverables:

- environment manifest (identity/build/runtime/worker/tool/approval capabilities)
- agent-visible maintenance request actions (restart, recycle worker, reload config, dependency refresh, operator attention)
- explicit forbidden action declarations

Control rule:

- agent can request privileged maintenance actions; operator/system policy decides execution

## Milestone 21: Worker Runtime Hotspot Refactor

Goal:

- reduce maintenance risk in runtime hotspots via incremental internal boundary extraction

Planned internal splits:

- worker facade
- runtime executor
- sandbox/session adapter
- prompt assembler
- tool execution and permission gate
- post-run pipeline
- result mapper

Approach:

- incremental extraction
- preserve existing contracts and behavior
- prioritize testability and reviewability

## Phase Sequencing Summary

Phase 1:

1. Milestone 15
2. Milestone A
3. Milestone 16
4. Milestone 17

Phase 2:

1. Milestone 18
2. Milestone 19

Phase 3:

1. Milestone 20
2. Milestone 21

## Open Planning Questions

1. which public product sentence remains canonical after milestone A rollout?
2. which runtime owns planning by default in production policy?
3. should scout mode launch as strictly read-only first?
4. which proposal categories (if any) can be auto-promoted?
5. which maintenance actions are request-only vs executable?
6. which hotspot refactors are highest leverage before autonomy expansion?

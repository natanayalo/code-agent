# Roadmap

## Planning Principles

- prioritize reliability, safety, and inspectability over feature breadth
- prefer runtime leverage (Codex/Gemini/OpenRouter capabilities) over rebuilding equivalent platform logic
- keep human-in-the-loop for trust-boundary and high-risk changes

## Current Phase

Phase 2: bounded autonomy.

Priority sequence:

1. Milestone 18: Controlled Autonomy / Scout Mode
2. Milestone 19: Reflection and Improvement Pipeline

Planned next phases:

1. Phase 3: deeper platform maturity

Past phases:

1. Phase 1: clarity and control (Milestones 15 through 17.5)

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

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-186 | P0 | Define Scout Mode task type and lane parameters. | Add `'scout'` to `TaskSpecType` or runtime mode. Map a lower-priority queue lane, define separate budget defaults. | Scout tasks can be created and routed without blocking primary task lane. | `orchestrator/state.py`, `apps/api/routes/tasks.py` | Queue starvation if priorities are not strict. | (Done in #222) |
| T-187 | P0 | Add Read-Mostly sandbox policy. | Create sandbox profile that only allows reading files. Disable modifying commands except writing to a designated artifact directory. | Scout tasks fail if they attempt `git commit` or `npm install`. | `sandbox/policy.py`, `workers/native_agent_runner.py` | Over-constraining might break some code analysis tools. | (Done in #223) |
| T-188 | P0 | Implement Idea Inbox / Proposal store. | Add `Proposal` DB model tied to `Session`. Allow tasks to emit ideas instead of final code. | Ideas are durably stored with origin metadata and status (`PENDING_REVIEW`). | `db/models.py`, `repositories/sqlalchemy.py` | Schema migration needs care. | (Done in #224) |
| T-189 | P0 | Route Scout output to Review Inbox. | Ensure Scout tasks do not merge or deploy. Their artifacts transition to a `PENDING_REVIEW` proposal state. | Scout outputs only show up in the Idea Inbox and never execute mutations on main codebase. | `orchestrator/execution.py`, `orchestrator/graph.py` | Escape via tool loop if boundaries are weak. | (Done in #226) |
| T-190 | P1 | Dashboard UI for Idea Inbox. | Surface Scout proposals in the dashboard. Operator can review, reject, or promote them to real tasks. | Operator can click "Accept Idea" to turn it into a queued execution task. | `dashboard/src/components/*`, `dashboard/src/services/api.ts` | State drift between UI and backend. | (Done in #227) |
| T-191 | P2 | Add Trigger Sources: Schedule and Idle time. | Create a cron-like scheduler or API endpoints that spawn Scout tasks based on configured intervals or system idleness. | Background task generation works without human input. | `apps/api/scheduler.py` | Spawning loops could consume budget quickly. | (Done) |
| T-200 | P0 | Skip change-oriented review for read-only tasks. | Change read-only execution policy so worker self-review and independent code-change review are skipped when `read_only=true` or no files changed; keep deterministic verification only when explicit safe commands exist. | Read-only Scout/investigation tasks no longer run diff-oriented review, while mutable tasks still do. | `workers/self_review.py`, `orchestrator/review.py`, `orchestrator/nodes/verification.py` | Must preserve useful verification for read-only investigations without running change-review prompts. | Done |
| T-201 | P1 | Add structured Scout trigger parameters and repo allowlist. | Extend Scout trigger request support for `mode`, `repo_key`, optional `branch`, `focus`, `depth`, and capped `max_proposals`; resolve `repo_key` through configured allowlisted repos instead of arbitrary repo URLs. | Default no-body trigger still works; configured repo keys resolve safely; research mode rejects missing focus; invalid/capped params return clear validation errors. | `apps/api/routes/tasks.py`, `apps/api/config.py`, dashboard trigger UI/tests | Input flexibility could expand remote-code intake; keep repo selection allowlisted and server-controlled. | (Done in #263) |
| T-202 | P1 | Add task-type prompt overlays for Scout modes. | Add prompt overlays on top of the shared worker prompt for `repo_scout`, `research_scout`, and `deep_scout`, with proposal-oriented output rules, evidence requirements, and read-only guardrails. | Scout prompts are mode-specific, generic workers keep the shared base prompt, and tests assert the expected overlay content. | `workers/prompt.py`, worker prompt tests | Prompt drift can make task types inconsistent; keep overlays small and composable. | Done |
| T-203 | P1 | Implement Repo Scout and Research Scout modes. | Route `mode=repo` to read-only repository inspection and `mode=research` to topic-driven research proposal generation with explicit focus/topic input. | Repo Scout produces repo-evidenced proposals; Research Scout produces source-aware proposals; both land in Idea Inbox with mode metadata. | `orchestrator/task_spec.py`, `orchestrator/execution_outcome_service.py`, Scout API/dashboard tests | Research Scout needs explicit network/source policy to avoid noisy or stale recommendations. | Planned |
| T-204 | P2 | Implement Deep Scout chaining. | Add explicit `mode=deep` / `repo_then_research` flow that chains repo inspection into targeted research with a higher but still capped budget. | Deep Scout runs only when explicitly requested, records chain metadata, and produces a richer Idea Inbox proposal without mutating code. | `orchestrator/graph.py`, `orchestrator/execution_outcome_service.py`, Scout integration tests | Chaining can consume budget quickly; require explicit operator intent and strict caps. | Planned |

## Milestone 19: Reflection and Improvement Pipeline

Goal:

- convert execution friction into structured, reviewable improvement proposals

Planned deliverables:

- friction report schema
- improvement suggestion schema
- proposal scoring/planning by value, effort, risk, layer impact, validation path, and HITL need
- review queue for improvement proposals

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-192 | P0 | Define Reflection and Improvement schemas. | Add Pydantic models for `FrictionReport` and `ImprovementSuggestion` (including scoring fields: value, effort, risk, layer_impact, validation_path, hitl_need). | Schemas validate successfully and have robust type definitions. | `orchestrator/reflection.py` (new), `tests/unit/test_reflection_schemas.py` | Schema definitions might drift without strict Pydantic enforcement. | (Done) |
| T-193 | P0 | Integrate schemas with DB Proposal model. | Add a `proposal_type` column/enum to distinguish reflection improvements from scout ideas. Extend JSON payload mapping. | DB migration applies cleanly; old `Proposal` rows default to `'scout'`. | `db/models.py`, `db/enums.py`, `repositories/sqlalchemy.py` | DB migration schema evolution needs care. | Completed |
| T-194 | P1 | Capture execution friction from worker runtime. | Modify the worker failure/verifier rejection paths to automatically emit a `FrictionReport` capturing the context, source, and impact. | Friction reports are generated automatically on repeated command/test failures. | `workers/native_agent_runner.py`, `orchestrator/verification.py` | Too much noise if we report on every single small failure. | Completed |
| T-195 | P1 | Generate Scored Improvement Proposals. | Use deterministic synthesis to analyze `FrictionReport`s and produce scored `ImprovementSuggestion`s with effort/risk scoring. | Emits actionable proposals stored as `PENDING_REVIEW` in the DB. | `orchestrator/improvement_suggestions.py`, `orchestrator/execution_outcome_service.py` | Scoring may require future LLM enrichment, but the first slice avoids model latency. | Completed |
| T-196 | P1 | Add LLM-Based Improvement Proposal Scoring. | Use an optional model-backed scorer to generate or revise `ImprovementSuggestion` scoring fields and rationale from `FrictionReport` evidence; keep deterministic scoring as fallback. | Feature flag controls LLM scoring; failed/time-out model calls fall back to deterministic suggestions; metadata records model rationale and fallback status; tests cover success, fallback, and disabled-flag behavior. | `orchestrator/improvement_suggestions.py`, `orchestrator/brain.py`, `orchestrator/execution_outcome_service.py` | Model latency and nondeterminism can make proposal quality inconsistent. | Completed |
| T-197 | P1 | Dashboard UI for Reflection & Improvement Queue. | Extend the Idea Inbox UI to also display Friction Reports and Improvement Suggestions with their new scoring fields. | Operators can view, approve, or reject structural improvements. | `dashboard/src/components/*`, `dashboard/src/services/api.ts` | UI clutter if too many proposals are generated. | Completed |
| T-198 | P1 | Add dashboard trigger tab for task and scout actions. | Add a dedicated dashboard view for operator-triggered actions using existing APIs, including generic task submission shortcuts and `/tasks/scout/trigger`; do not add a manual reflection trigger in this slice because reflection remains outcome-driven. | Operator can trigger configured Scout runs and submit task-style trigger actions from the dashboard; UI shows clear success/error/loading states and does not expose secrets. | `dashboard/src/components/*`, `dashboard/src/services/api.ts`, dashboard tests | Trigger controls could accidentally encourage budget-heavy runs; keep controls explicit and scoped to existing authenticated APIs. | Completed |
| T-199 | P1 | Full dashboard QA, visual polish, limits, and reusable QA skill. | Audit the full dashboard in browser across core routes; fix high-confidence bugs, interaction rough edges, visual overflow, empty/error/loading states, and obvious performance issues; document the repeatable workflow as a repo-local dashboard QA skill. | Dashboard QA produces verified fixes, browser evidence, coverage remains above threshold, and `.agents/skills/` contains a reusable QA workflow for future dashboard passes. | `dashboard/src/*`, `.agents/skills/*`, dashboard tests | Scope can sprawl; prioritize bugs, usability regressions, visual limits, and reusable verification over broad redesign. | Planned |

Manual-only zones:

- auth/security
- secrets/sandbox boundaries
- approval core logic
- deployment/billing controls

## Milestone 19.5: Gemini to Antigravity Migration

Goal:

- make Antigravity CLI the canonical public worker identity and migration target for the current Gemini CLI worker lane

Planned deliverables:

- canonical `antigravity` worker type and profile names
- Antigravity native CLI adapter using `agy -p` / `agy --print`
- prompt-as-argv support in the native runner for CLIs that require it
- Docker worker support using official Antigravity install/auth mechanisms
- updated e2e QA, runbook, compose, env, dashboard/API labels, and operator guidance
- removal of Gemini CLI defaults after Antigravity Docker e2e is proven

Public interface direction:

- canonical worker type becomes `antigravity`; `gemini` is not the long-term public name
- canonical profiles become `antigravity-native-executor`, `antigravity-native-executor-read-only`, `antigravity-native-planner`, `antigravity-native-reviewer`, and `antigravity-native-discovery`
- Antigravity env vars are `CODE_AGENT_ANTIGRAVITY_CLI_BIN`, `CODE_AGENT_ANTIGRAVITY_MODEL`, `CODE_AGENT_ANTIGRAVITY_TIMEOUT_SECONDS`, `CODE_AGENT_ANTIGRAVITY_AUTH_DIR`, `CODE_AGENT_ANTIGRAVITY_NATIVE_SANDBOX_ENABLED`, `CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION`, and `CODE_AGENT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY`
- runtime worker inputs use only the canonical `antigravity` identity; historical `gemini` rows are handled by migration

Manual-derived constraints:

- `agy` one-shot automation uses prompt-as-argv (`agy -p "<prompt>"`, also exposed locally as `--print`); command logging must redact prompt text because it is no longer stdin-only
- Antigravity stores preferences and permissions in `~/.gemini/antigravity-cli/settings.json`, including `toolPermission`, `artifactReviewPolicy`, and `enableTerminalSandbox`
- supported permission modes include `request-review`, `proceed-in-sandbox`, `always-proceed`, and `strict`; the platform must map worker profiles to these modes explicitly instead of relying on interactive prompts
- Antigravity auth uses the operating-system secure keyring (Apple Keychain, Linux Secret Service over DBus, or Windows Credential Manager), so `CODE_AGENT_ANTIGRAVITY_AUTH_DIR` must not imply host keychain copying or secret scraping
- Antigravity parses workspace `AGENTS.md`; migration docs must also cover legacy plugin import, skills paths, and MCP config movement into `.agents/`
- desktop app installation can share settings with the CLI, but it does not by itself prove auth is available inside a Linux Docker worker

Reference manuals:

- [Antigravity CLI overview](https://antigravity.google/docs/cli-overview)
- [Antigravity CLI reference](https://antigravity.google/docs/cli-reference)
- [Antigravity CLI install](https://antigravity.google/docs/cli-install)
- [Antigravity CLI getting started](https://antigravity.google/docs/cli-getting-started)
- [Antigravity CLI troubleshooting](https://antigravity.google/docs/cli-troubleshooting)
- [Antigravity CLI best practices](https://antigravity.google/docs/cli-best-practices)
- [Antigravity CLI sandbox](https://antigravity.google/docs/cli-sandbox)
- [Antigravity CLI permissions](https://antigravity.google/docs/cli-permissions)
- [Gemini CLI to Antigravity migration](https://antigravity.google/docs/gcli-migration)

Task list:

| ID | Priority | Description | Implementation notes | Acceptance criteria | Likely touched files | Risks / dependencies | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T-205 | P0 | Rename canonical worker identity from Gemini to Antigravity. | Add `antigravity` worker type, profile names, API/dashboard labels, and DB enum/check-constraint migration from persisted `gemini` rows to `antigravity`. Keep `gemini` only in historical migrations and the current implementation adapter until later Antigravity adapter tasks land. | Existing `gemini` task/run rows upgrade to `antigravity`; new submissions accept only the canonical `antigravity` worker identity. | `db/enums.py`, `workers/base.py`, `db/migrations/versions/*`, routing/API tests | Broad type/name churn can break replay, snapshots, and dashboards if aliases are inconsistent. | Done |
| T-206 | P0 | Add Antigravity native CLI adapter. | Build an adapter around `agy -p` / `agy --print <prompt> --print-timeout ... --model ... --log-file ...`; generate or mount the per-run Antigravity settings needed for `toolPermission`, `artifactReviewPolicy`, and `enableTerminalSandbox`; map stdout, JSON responses, provider/auth errors, timeout, diff, and changed files into `WorkerResult`. | Fake `agy` tests cover success, JSON output, timeout, auth/provider failure, permission prompt/denial, no-change success, settings generation, and changed-file collection. | `workers/*antigravity*`, `apps/api/task_service_factory.py`, worker tests | `agy -p` / `--print` consumes the prompt as an argument and differs from Gemini CLI flags, permissions, and auth behavior; it must not be treated as a drop-in binary rename. | Done |
| T-207 | P0 | Support prompt-as-argv in native runner. | Extend native-agent execution so adapters can place the prompt in argv while existing CLIs keep stdin prompt delivery. Redact prompt text from logged/sanitized command strings. | Native runner tests prove stdin remains default, `agy` prompt-in-argv works, command logs do not expose full prompt/secrets, and timeouts still collect artifacts. | `workers/native_agent_runner.py`, `workers/native_agent_models.py`, runner tests | Long prompts can hit argv limits; adapter must fail clearly or use a bounded strategy. | Done |
| T-208 | P0 | Add Docker Antigravity support. | Install Antigravity CLI in the worker image using the official CLI install path and prove auth works inside compose through official Antigravity keyring mechanisms. For Linux containers, validate Secret Service/DBus requirements or document the official blocker. Do not invent keychain bypasses or scrape host secrets. | Docker smoke passes for `agy models` and `agy --print 'Reply with OK only'`; permission settings are deterministic for non-interactive runs; if official non-interactive auth is unavailable, the blocker is documented and the milestone remains incomplete. | `Dockerfile.worker`, `docker-compose.yml`, `.env.example`, Docker/e2e scripts | Antigravity desktop/keychain auth may not transfer safely into Linux containers, and headless DBus/keyring support may require an official container-friendly auth path. | Done |
| T-209 | P1 | Update e2e QA and operator docs. | Replace Gemini defaults and auth guidance with Antigravity guidance in e2e scripts, README, runbook, compose docs, and env examples. Include `agy` install/PATH guidance, keyring/DBus troubleshooting, permission presets, `AGENTS.md` context behavior, legacy plugin import, skills path migration, and MCP config relocation. | Local e2e instructions use `worker_override=antigravity`; stale `gemini auth login` guidance is removed or marked legacy; operator docs explain how to diagnose `agy: command not found`, locked keyrings, and permission-prompt timeouts. | `.agents/skills/e2e-qa/scripts/*`, `README.md`, `docs/runbook.md`, `.env.example` | Docs can drift if code-level aliases remain during the migration bridge. | Done |
| T-210 | P1 | Update dashboard/API worker labels. | Update operator-visible worker/profile labels and frontend/API contract fixtures to show `antigravity` names. | Dashboard and API tests cover `worker_override=antigravity`, Antigravity profiles, and canonical display behavior. | `dashboard/src/*`, `apps/api/routes/*`, API/dashboard tests | UI/API compatibility must be explicit for existing saved tasks and replays. | Done |
| T-211 | P1 | Remove Gemini CLI defaults after Antigravity e2e passes. | Drop Gemini CLI from the default worker image/config and leave only documented temporary aliases if still needed. | Default compose image uses Antigravity, full webhook e2e passes with `antigravity`, and Gemini CLI is no longer required for local happy path. | `Dockerfile.worker`, `docker-compose.yml`, docs/tests | Removing Gemini too early can break enterprise/API-key users before the migration bridge is verified. | Done |

Testing and acceptance:

- unit tests cover worker enum/profile validation, service factory wiring, Antigravity command construction, env allowlist, and native-runner prompt delivery mode
- migration integration tests prove existing `gemini` task/run rows upgrade to `antigravity` and constraints accept the new canonical value set
- fake `agy` tests cover success, JSON output, timeout, auth/provider failure, permission prompt/denial, settings generation, and changed-file collection
- Docker smoke covers `agy models` and `agy --print 'Reply with OK only'` inside the worker container
- full webhook e2e passes with `worker_override=antigravity` after Docker auth is proven
- dashboard/API contract tests cover worker override and profile names

Assumptions:

- Milestone 19 remains active; Milestone 19.5 is the migration bridge before Phase 3
- existing Milestones 20 and 21 keep their numbers
- Docker support is required for the milestone to complete, but auth must use official Antigravity mechanisms only
- if official Antigravity CLI cannot authenticate non-interactively in Docker, T-208 documents the blocker and the milestone remains incomplete rather than shipping an unsafe workaround

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
3. Milestone 19.5

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

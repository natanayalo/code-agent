# Changelog

This file is generated from merged pull requests with
[git-cliff](https://git-cliff.org/). Do not use `docs/status.md` as a
completed-task ledger; keep completed work here instead.
## Merged PR History

### Added

- Feat: add memory admission spike ([#297](https://github.com/natanayalo/code-agent/pull/297))

- Feat: add reviewable memory retrieval corpus ([#296](https://github.com/natanayalo/code-agent/pull/296))

- Feat: add memory retrieval evaluation ([#295](https://github.com/natanayalo/code-agent/pull/295))

- Feat: add memory full-text search visibility ([#294](https://github.com/natanayalo/code-agent/pull/294))

- Feat: wire memory load and persistence ([#293](https://github.com/natanayalo/code-agent/pull/293))

- Feat: implement Milestone 22 dynamic performance-based worker routing ([#292](https://github.com/natanayalo/code-agent/pull/292))

- Feat: integrate WorkerFacade to centralize worker routing ([#290](https://github.com/natanayalo/code-agent/pull/290))

- Feat: rollout M20.7 frozen evaluation suite ([#289](https://github.com/natanayalo/code-agent/pull/289))

- Feat: implement M20.5 repo validation profiles and repo url lockdown ([#287](https://github.com/natanayalo/code-agent/pull/287))

- Feat: add human interactions pending API and inbox UI data ([#279](https://github.com/natanayalo/code-agent/pull/279))

- Feat: implement execution plan spine for M20.2 ([#277](https://github.com/natanayalo/code-agent/pull/277))

- Feat: retain runtime manifest in worker_runs ([#276](https://github.com/natanayalo/code-agent/pull/276))

- Feat(evaluation): introduce M20.0 reliability metrics and reporting ([#275](https://github.com/natanayalo/code-agent/pull/275))

- Feat: implement deep scout chaining architecture ([#266](https://github.com/natanayalo/code-agent/pull/266))

- Feat: implement Repo and Research Scout mode metadata (T-203) ([#265](https://github.com/natanayalo/code-agent/pull/265))

- Feat: add structured Scout trigger parameters and repo allowlist (T-201) ([#263](https://github.com/natanayalo/code-agent/pull/263))

- Feat: skip change-oriented reviews for read-only tasks (T-200) ([#262](https://github.com/natanayalo/code-agent/pull/262))

- Feat: remove Gemini CLI defaults and migrate to Antigravity (T-211) ([#260](https://github.com/natanayalo/code-agent/pull/260))

- Feat: coerce legacy gemini worker types to antigravity ([#259](https://github.com/natanayalo/code-agent/pull/259))

- Feat(ui): format worker labels for antigravity migration [T-210] ([#258](https://github.com/natanayalo/code-agent/pull/258))

- Feat: add Docker Antigravity support using official install mechanisms ([#254](https://github.com/natanayalo/code-agent/pull/254))

- Feat: add Antigravity native CLI adapter ([#253](https://github.com/natanayalo/code-agent/pull/253))

- Feat: rename worker identity to antigravity ([#252](https://github.com/natanayalo/code-agent/pull/252))

- Feat: add dashboard trigger actions ([#250](https://github.com/natanayalo/code-agent/pull/250))

- Feat: add reflection improvement review queue ([#249](https://github.com/natanayalo/code-agent/pull/249))

- Feat: add llm improvement proposal scoring ([#248](https://github.com/natanayalo/code-agent/pull/248))

- Feat: generate scored improvement proposals ([#247](https://github.com/natanayalo/code-agent/pull/247))

- Feat: capture execution friction from worker runtime (T-194) ([#237](https://github.com/natanayalo/code-agent/pull/237))

- Feat: implement scout vs reflection proposal distinction (T-193) ([#236](https://github.com/natanayalo/code-agent/pull/236))

- Feat: define reflection and improvement schemas (T-192) ([#235](https://github.com/natanayalo/code-agent/pull/235))

- Feat: implement schedule and idle time trigger sources for scout mode ([#234](https://github.com/natanayalo/code-agent/pull/234))

- Feat(proposals): complete T-190 Idea Inbox Page ([#227](https://github.com/natanayalo/code-agent/pull/227))

- Feat: Route Scout output to Review Inbox Proposals ([#226](https://github.com/natanayalo/code-agent/pull/226))

- Feat: implement idea inbox / proposal store (T-188) ([#224](https://github.com/natanayalo/code-agent/pull/224))

- Feat: implement read-mostly sandbox policy (T-187) ([#223](https://github.com/natanayalo/code-agent/pull/223))

- Feat: implement scout mode task lane and bounds (T-186) ([#222](https://github.com/natanayalo/code-agent/pull/222))

- Feat: orchestrator delivery node refactor & strict git enforcement ([#220](https://github.com/natanayalo/code-agent/pull/220))

- Feat: optimize discovery latency and brain-router fallback (T-177) ([#216](https://github.com/natanayalo/code-agent/pull/216))

- Feat: standardise tracing spans and fix e2e QA ([#214](https://github.com/natanayalo/code-agent/pull/214))

- Feat: implement snapshot retries for orchestrator workspace runs ([#213](https://github.com/natanayalo/code-agent/pull/213))

- Feat: add goal-prompt-creator skill and regression tests ([#202](https://github.com/natanayalo/code-agent/pull/202))

- Feat(db): add environment, workspace and task spec migrations ([#197](https://github.com/natanayalo/code-agent/pull/197))

- Feat(sandbox): enhance container and workspace lifecycle execution ([#198](https://github.com/natanayalo/code-agent/pull/198))

- Feat(workers): update cli workers and native agent runner execution ([#199](https://github.com/natanayalo/code-agent/pull/199))

- Feat(orchestrator): refactor graph, brain and provisioning execution ([#200](https://github.com/natanayalo/code-agent/pull/200))

- Feat(api): wire up orchestrator, endpoints and observability ([#201](https://github.com/natanayalo/code-agent/pull/201))

- Feat: refactor orchestrator graph into modular nodes and implement ve… ([#188](https://github.com/natanayalo/code-agent/pull/188))

- Feat: propagate task and session metadata throughout worker execution… ([#185](https://github.com/natanayalo/code-agent/pull/185))

- Feat: add infra-failure detection to NativeAgentRunner (T-175) ([#182](https://github.com/natanayalo/code-agent/pull/182))

- Feat: stabilize deterministic verification pipeline and harden shell worker ([#180](https://github.com/natanayalo/code-agent/pull/180))

- Feat: simplify native-agent prompts and enforce delivery_mode (T-173) ([#179](https://github.com/natanayalo/code-agent/pull/179))

- Feat: implement tracing guardrails for native agent runs (T-170) ([#176](https://github.com/natanayalo/code-agent/pull/176))

- Feat: stabilize dashboard interaction and cancel controls in task detail ([#175](https://github.com/natanayalo/code-agent/pull/175))

- Feat: harden task cancellation semantics (T-165) ([#173](https://github.com/natanayalo/code-agent/pull/173))

- Feat: T-164 native runner contract repair and normalization ([#172](https://github.com/natanayalo/code-agent/pull/172))

- Feat: add brain-guided retry and verifier acceptance clamps (T-163) ([#170](https://github.com/natanayalo/code-agent/pull/170))

- Feat: hard-deprecate codex/gemini tool-loop defaults and add explicit legacy opt-in ([#169](https://github.com/natanayalo/code-agent/pull/169))

- Feat: surface native run observability in task detail ([#168](https://github.com/natanayalo/code-agent/pull/168))

- Feat: implement async model-backed orchestrator brain for task enrichment ([#167](https://github.com/natanayalo/code-agent/pull/167))

- Feat: add clamp-governed native planner route recommendations ([#166](https://github.com/natanayalo/code-agent/pull/166))

- Feat: add feature-flagged orchestrator brain task-spec enrichment ([#165](https://github.com/natanayalo/code-agent/pull/165))

- Feat: add bounded verifier repair handoff after failed verification ([#164](https://github.com/natanayalo/code-agent/pull/164))

- Feat: add baseline independent verifier execution stage (T-158) ([#163](https://github.com/natanayalo/code-agent/pull/163))

- Feat: gate clarification-required tasks before dispatch (T-157) ([#162](https://github.com/natanayalo/code-agent/pull/162))

- Feat: add Gemini native-agent execution mode ([#161](https://github.com/natanayalo/code-agent/pull/161))

- Feat: add Codex native runtime-mode execution path (T-155) ([#160](https://github.com/natanayalo/code-agent/pull/160))

- Feat: add native agent runner abstraction (T-154) ([#159](https://github.com/natanayalo/code-agent/pull/159))

- Feat: expose worker profile/runtime metadata for profiled tasks ([#158](https://github.com/natanayalo/code-agent/pull/158))

- Feat: add worker profile and runtime metadata persistence ([#156](https://github.com/natanayalo/code-agent/pull/156))

- Feat: add profile-aware worker routing selection ([#155](https://github.com/natanayalo/code-agent/pull/155))

- Feat: add worker runtime profile contract models ([#154](https://github.com/natanayalo/code-agent/pull/154))

- Feat: connect persisted trace metadata to dashboard UI (T-152) ([#153](https://github.com/natanayalo/code-agent/pull/153))

- Feat: Enhanced Observability with Manual Spans (T-151) ([#152](https://github.com/natanayalo/code-agent/pull/152))

- Feat: migrate observability bootstrap to phoenix.otel.register() ([#147](https://github.com/natanayalo/code-agent/pull/147))

- Feat: add phoenix OSS + OpenInference tracing bootstrap (T-150) ([#145](https://github.com/natanayalo/code-agent/pull/145))

- Feat: add trace observability to task detail UI (T-153) ([#143](https://github.com/natanayalo/code-agent/pull/143))

- Feat(api,dashboard): implement API & UI for Tool Inventory and Sandbox status ([#142](https://github.com/natanayalo/code-agent/pull/142))

- Feat: add knowledge base API and dashboard manager (T-144) ([#141](https://github.com/natanayalo/code-agent/pull/141))

- Feat: expose session working context in API and dashboard ([#140](https://github.com/natanayalo/code-agent/pull/140))

- Feat: robust task approval persistence and operator UI ([#139](https://github.com/natanayalo/code-agent/pull/139))

- Feat: add replay with overrides modal to dashboard ([#138](https://github.com/natanayalo/code-agent/pull/138))

- Feat: add task detail timeline/logs/artifacts view (T-138) ([#137](https://github.com/natanayalo/code-agent/pull/137))

- Feat: show TaskSpec and pending interactions in dashboard task detail/inbox ([#136](https://github.com/natanayalo/code-agent/pull/136))

- Feat: map TaskSpec clarification/permission into HumanInteraction records ([#135](https://github.com/natanayalo/code-agent/pull/135))

- Feat: add HumanInteraction model (T-147) ([#134](https://github.com/natanayalo/code-agent/pull/134))

- Feat: add TaskSpec foundation ([#133](https://github.com/natanayalo/code-agent/pull/133))

- Feat: implement dashboard routing and pages for Sessions/Metrics (T-137) ([#132](https://github.com/natanayalo/code-agent/pull/132))

- Feat(auth): secure HttpOnly cookie authentication for dashboard ([#123](https://github.com/natanayalo/code-agent/pull/123))

- Feat: implement task replay control (unchanged) ([#122](https://github.com/natanayalo/code-agent/pull/122))

- Feat: implement approval/rejection UI components in dashboard (T-134) ([#121](https://github.com/natanayalo/code-agent/pull/121))

- Feat(dashboard): add CI and comprehensive tests ([#120](https://github.com/natanayalo/code-agent/pull/120))

- Feat: implement core dashboard layout and task status board (T-132) ([#119](https://github.com/natanayalo/code-agent/pull/119))

- Feat: implement API endpoints for task/session listing and detailed view ([#118](https://github.com/natanayalo/code-agent/pull/118))

- Feat(dashboard): design PWA architecture and initialize project ([#114](https://github.com/natanayalo/code-agent/pull/114))

- Feat: add flagged role-native messaging slice for OpenRouter adapter (T-128) ([#110](https://github.com/natanayalo/code-agent/pull/110))

- Feat: centralize runtime adapter tool guidance ([#109](https://github.com/natanayalo/code-agent/pull/109))

- Feat: T-123 shared adapter prompt override for review-mode ([#104](https://github.com/natanayalo/code-agent/pull/104))

- Feat: extend frozen eval harness for reviewer quality ([#103](https://github.com/natanayalo/code-agent/pull/103))

- Feat: add bounded independent-review repair handoff loop ([#102](https://github.com/natanayalo/code-agent/pull/102))

- Feat: add independent reviewer finding suppression and gating ([#101](https://github.com/natanayalo/code-agent/pull/101))

- Feat: implement independent reviewer stage and persistence ([#100](https://github.com/natanayalo/code-agent/pull/100))

- Feat: add review-specific prompt assembly for self-review ([#99](https://github.com/natanayalo/code-agent/pull/99))

- Feat: add targeted reviewer context packet for self-review (T-115) ([#98](https://github.com/natanayalo/code-agent/pull/98))

- Feat: add context-window preflight guard to cli runtime ([#96](https://github.com/natanayalo/code-agent/pull/96))

- Feat: add bounded worker self-review backstop ([#95](https://github.com/natanayalo/code-agent/pull/95))

- Feat: add shared review result schema and persistence ([#94](https://github.com/natanayalo/code-agent/pull/94))

- Feat: add structured failure taxonomy for routing and recovery ([#93](https://github.com/natanayalo/code-agent/pull/93))

- Feat: add deterministic context condenser to CLI runtime ([#92](https://github.com/natanayalo/code-agent/pull/92))

- Feat: parallelize frozen-suite evaluation execution ([#90](https://github.com/natanayalo/code-agent/pull/90))

- Feat: migrate frozen suite payload validation to pydantic ([#89](https://github.com/natanayalo/code-agent/pull/89))

- Feat: add frozen evaluation harness with orchestrator runner ([#82](https://github.com/natanayalo/code-agent/pull/82))

- Feat(workers): add shared post-run lint/format step ([#81](https://github.com/natanayalo/code-agent/pull/81))

- Feat(retention): add run expiry and align workspace root handling ([#80](https://github.com/natanayalo/code-agent/pull/80))

- Feat: add runtime quotas and unattended budget defaults ([#78](https://github.com/natanayalo/code-agent/pull/78))

- Feat: implement secret scoping for sandbox hardening (T-100) ([#75](https://github.com/natanayalo/code-agent/pull/75))

- Feat: implement operational metrics (T-092) ([#74](https://github.com/natanayalo/code-agent/pull/74))

- Feat: implement task replay mechanism (T-091) ([#73](https://github.com/natanayalo/code-agent/pull/73))

- Feat: add task timeline tracking and persistence (T-090) ([#72](https://github.com/natanayalo/code-agent/pull/72))

- Feat: add paused-task approval decision endpoint ([#71](https://github.com/natanayalo/code-agent/pull/71))

- Feat(runtime): production-like split api/worker runtime and approval persistence hardening ([#70](https://github.com/natanayalo/code-agent/pull/70))

- Feat(prompt): inject bounded build and CI repo context ([#64](https://github.com/natanayalo/code-agent/pull/64))

- Feat(tools): add structured file editing and search tools ([#63](https://github.com/natanayalo/code-agent/pull/63))

- Feat: inject .agents workspace guidance into worker prompts ([#62](https://github.com/natanayalo/code-agent/pull/62))

- Feat(tools): add browser search wrapper ([#61](https://github.com/natanayalo/code-agent/pull/61))

- Feat(tools): add structured GitHub wrapper ([#60](https://github.com/natanayalo/code-agent/pull/60))

- Feat: add execute_git utility wrapper ([#57](https://github.com/natanayalo/code-agent/pull/57))

- Feat: add shared outbound notifier clients ([#51](https://github.com/natanayalo/code-agent/pull/51))

- Feat: add milestone 10 progress replies and dedupe ([#50](https://github.com/natanayalo/code-agent/pull/50))

- Feat(telegram): T-051 add Telegram webhook adapter mapping Updates to… ([#49](https://github.com/natanayalo/code-agent/pull/49))

- Feat(webhook): T-050 add generic webhook adapter translating JSON pay… ([#48](https://github.com/natanayalo/code-agent/pull/48))

- Feat(orchestrator): T-071 routing heuristics + T-072 manual override ([#47](https://github.com/natanayalo/code-agent/pull/47))

- Feat: add GeminiCliWorker and GeminiCliRuntimeAdapter as second worke… ([#46](https://github.com/natanayalo/code-agent/pull/46))

- Feat: add structured worker run observability ([#45](https://github.com/natanayalo/code-agent/pull/45))

- Feat: implement skeptical memory schema and session state ([#42](https://github.com/natanayalo/code-agent/pull/42))

- Feat(sandbox): harden execution boundary and auditability (T-054) ([#40](https://github.com/natanayalo/code-agent/pull/40))

- Feat: T-055 add constrained verifier stage to orchestrator ([#39](https://github.com/natanayalo/code-agent/pull/39))

- Feat: implement richer worker timeout diagnostics for T-042 ([#38](https://github.com/natanayalo/code-agent/pull/38))

- Feat(orchestrator): add permission escalation pause/resume edge ([#37](https://github.com/natanayalo/code-agent/pull/37))

- Feat: implement codex exec cli adapter and task service wiring ([#36](https://github.com/natanayalo/code-agent/pull/36))

- Feat: implement T-044 task submission and status API ([#35](https://github.com/natanayalo/code-agent/pull/35))

- Feat: add orchestrator worker timeout envelope ([#34](https://github.com/natanayalo/code-agent/pull/34))

- Feat: add cli runtime permission and budget guards ([#33](https://github.com/natanayalo/code-agent/pull/33))

- Feat: add explicit tool registry for cli runtime ([#32](https://github.com/natanayalo/code-agent/pull/32))

- Feat: add shared cli runtime scaffold ([#31](https://github.com/natanayalo/code-agent/pull/31))

- Feat: add structured worker prompt builder ([#28](https://github.com/natanayalo/code-agent/pull/28))

- Feat: add persistent sandbox shell sessions ([#26](https://github.com/natanayalo/code-agent/pull/26))

- Feat: add initial codex worker adapter ([#23](https://github.com/natanayalo/code-agent/pull/23))

- Feat: define shared worker interface ([#22](https://github.com/natanayalo/code-agent/pull/22))

- Feat: add sandbox artifact capture ([#20](https://github.com/natanayalo/code-agent/pull/20))

- Feat(sandbox): implement T-030 per-task workspace manager and lifecyc… ([#17](https://github.com/natanayalo/code-agent/pull/17))

- Feat: add orchestrator checkpoint persistence ([#15](https://github.com/natanayalo/code-agent/pull/15))

- Feat: normalize persistence enums and constrained fields ([#14](https://github.com/natanayalo/code-agent/pull/14))

- Feat: add langgraph workflow skeleton ([#13](https://github.com/natanayalo/code-agent/pull/13))

- Feat: add orchestrator state schema ([#12](https://github.com/natanayalo/code-agent/pull/12))

- Feat: add sqlalchemy repository layer ([#11](https://github.com/natanayalo/code-agent/pull/11))

- Feat: add local docker infrastructure ([#5](https://github.com/natanayalo/code-agent/pull/5))


### CI

- Ci: skip changelog-only validation runs ([#245](https://github.com/natanayalo/code-agent/pull/245))

- Ci: use deploy key for changelog push ([#244](https://github.com/natanayalo/code-agent/pull/244))

- Ci: commit generated changelog updates ([#243](https://github.com/natanayalo/code-agent/pull/243))

- Ci: split unit coverage and tighten pre-commit imports ([#157](https://github.com/natanayalo/code-agent/pull/157))


### Changed

- [codex] Add memory observations ([#298](https://github.com/natanayalo/code-agent/pull/298))

- Refactor: extract RuntimeExecutor and SandboxSessionAdapter to clean up CLI workers ([#291](https://github.com/natanayalo/code-agent/pull/291))

- [codex] add GitHub CI repair loop ([#288](https://github.com/natanayalo/code-agent/pull/288))

- [codex] add worker registry backpressure ([#280](https://github.com/natanayalo/code-agent/pull/280))

- Docs/m20.2 completed ([#278](https://github.com/natanayalo/code-agent/pull/278))

- [codex] Add runtime operating contract ([#274](https://github.com/natanayalo/code-agent/pull/274))

- Feat: add task-type prompt overlays for Scout modes (T-202) ([#264](https://github.com/natanayalo/code-agent/pull/264))

- [codex] Add dashboard QA skill and polish edge states ([#251](https://github.com/natanayalo/code-agent/pull/251))

- Feat/trigger sources t 191 ([#233](https://github.com/natanayalo/code-agent/pull/233))

- Refactor: reduce python size-check waivers for workers and orchestrator ([#212](https://github.com/natanayalo/code-agent/pull/212))

- Refactor: reduce python size-check waivers for workers and orchestrator ([#212](https://github.com/natanayalo/code-agent/pull/212))

- Refactor: systematically extract methods to satisfy file and function size limits ([#206](https://github.com/natanayalo/code-agent/pull/206))

- Refactor: reduce python size-check waivers ([#205](https://github.com/natanayalo/code-agent/pull/205))

- Refactor: enforce python code size limits by splitting large files ([#204](https://github.com/natanayalo/code-agent/pull/204))

- Task/t 172 codex sandbox alignment ([#177](https://github.com/natanayalo/code-agent/pull/177))

- Fix dashboard security vulnerabilities ([#117](https://github.com/natanayalo/code-agent/pull/117))

- Implement worker routing policy and stall-aware runtime stop reasons ([#112](https://github.com/natanayalo/code-agent/pull/112))

- Harden CLI runtime adapter behavior and tune execution budgets ([#111](https://github.com/natanayalo/code-agent/pull/111))

- T-126: Decompose CLI worker _run_sync into focused phases ([#108](https://github.com/natanayalo/code-agent/pull/108))

- Refactor: extract shared self-review + fix-loop coordinator ([#107](https://github.com/natanayalo/code-agent/pull/107))

- T-124: extract shared changed-files + post-run lint helper ([#105](https://github.com/natanayalo/code-agent/pull/105))

- T-073: add OpenRouter API client adapter ([#97](https://github.com/natanayalo/code-agent/pull/97))

- [codex] Add complex-task planning step before worker dispatch ([#91](https://github.com/natanayalo/code-agent/pull/91))

- T-120: convert frozen eval harness to async runner protocol ([#88](https://github.com/natanayalo/code-agent/pull/88))

- Resolve security alerts and migrate to Poetry 2.0 ([#83](https://github.com/natanayalo/code-agent/pull/83))

- Refine reviewer backlog sequencing ([#79](https://github.com/natanayalo/code-agent/pull/79))

- T-101 slice 1: enforce canonical permission escalation classes ([#77](https://github.com/natanayalo/code-agent/pull/77))

- [codex] add MCP-ready tool client abstraction ([#56](https://github.com/natanayalo/code-agent/pull/56))

- [codex] add API authentication for inbound routes ([#55](https://github.com/natanayalo/code-agent/pull/55))

- [codex] harden callback SSRF validation ([#53](https://github.com/natanayalo/code-agent/pull/53))

- [codex] isolate parallel progress notifier delivery ([#52](https://github.com/natanayalo/code-agent/pull/52))

- Codex/t 022 approval interrupt node ([#16](https://github.com/natanayalo/code-agent/pull/16))

- Codex/t 010 db models ([#6](https://github.com/natanayalo/code-agent/pull/6))

- Add health and readiness endpoints ([#3](https://github.com/natanayalo/code-agent/pull/3))

- Add initial coding-agent docs and repo guidance ([#1](https://github.com/natanayalo/code-agent/pull/1))


### Dependencies

- Chore(deps): bump openai from 2.43.0 to 2.44.0 ([#285](https://github.com/natanayalo/code-agent/pull/285))

- Chore(deps): bump alembic from 1.18.4 to 1.18.5 ([#284](https://github.com/natanayalo/code-agent/pull/284))

- Chore(deps): bump fastapi from 0.138.0 to 0.138.2 ([#283](https://github.com/natanayalo/code-agent/pull/283))

- Chore(deps-dev): bump pytest from 9.1.0 to 9.1.1 ([#282](https://github.com/natanayalo/code-agent/pull/282))

- Chore(deps): bump actions/cache from 5 to 6 ([#281](https://github.com/natanayalo/code-agent/pull/281))

- Chore(deps): bump openai from 2.41.1 to 2.43.0 ([#273](https://github.com/natanayalo/code-agent/pull/273))

- Chore(deps): bump fastapi from 0.137.0 to 0.138.0 ([#272](https://github.com/natanayalo/code-agent/pull/272))

- Chore(deps): bump langchain-core from 1.4.7 to 1.4.8 ([#271](https://github.com/natanayalo/code-agent/pull/271))

- Chore(deps): bump sqlalchemy from 2.0.50 to 2.0.51 ([#270](https://github.com/natanayalo/code-agent/pull/270))

- Chore(deps): bump langgraph from 1.2.4 to 1.2.6 ([#269](https://github.com/natanayalo/code-agent/pull/269))

- Chore(deps): bump actions/checkout from 6 to 7 ([#268](https://github.com/natanayalo/code-agent/pull/268))

- Chore(deps): bump langsmith from 0.8.4 to 0.8.18 ([#257](https://github.com/natanayalo/code-agent/pull/257))

- Chore(deps-dev): bump undici from 7.25.0 to 7.28.0 in /dashboard ([#255](https://github.com/natanayalo/code-agent/pull/255))

- Chore(deps): bump cryptography from 46.0.7 to 48.0.1 ([#241](https://github.com/natanayalo/code-agent/pull/241))

- Chore(deps): bump starlette from 1.2.1 to 1.3.1 ([#240](https://github.com/natanayalo/code-agent/pull/240))

- Chore(deps-dev): bump js-yaml from 4.1.1 to 4.2.0 in /dashboard ([#239](https://github.com/natanayalo/code-agent/pull/239))

- Chore(deps): bump langgraph-checkpoint from 4.1.0 to 4.1.1 ([#232](https://github.com/natanayalo/code-agent/pull/232))

- Chore(deps): bump langchain-core from 1.4.1 to 1.4.7 ([#231](https://github.com/natanayalo/code-agent/pull/231))

- Chore(deps): bump fastapi from 0.136.1 to 0.137.0 ([#229](https://github.com/natanayalo/code-agent/pull/229))

- Chore(deps-dev): bump pytest from 9.0.3 to 9.1.0 ([#230](https://github.com/natanayalo/code-agent/pull/230))

- Chore(deps): bump openai from 2.41.0 to 2.41.1 ([#228](https://github.com/natanayalo/code-agent/pull/228))

- Chore(deps): bump esbuild, @vitejs/plugin-react, vite and vite-plugin-pwa in /dashboard ([#219](https://github.com/natanayalo/code-agent/pull/219))

- Chore(deps): bump langgraph from 1.1.10 to 1.2.4 ([#211](https://github.com/natanayalo/code-agent/pull/211))

- Chore(deps): bump langgraph-checkpoint-sqlite from 3.0.3 to 3.1.0 ([#210](https://github.com/natanayalo/code-agent/pull/210))

- Chore(deps): bump sqlalchemy from 2.0.49 to 2.0.50 ([#209](https://github.com/natanayalo/code-agent/pull/209))

- Chore(deps-dev): bump pytest-asyncio from 1.3.0 to 1.4.0 ([#208](https://github.com/natanayalo/code-agent/pull/208))

- Chore(deps): bump pyjwt from 2.12.1 to 2.13.0 ([#207](https://github.com/natanayalo/code-agent/pull/207))

- Chore(deps): bump starlette from 1.0.0 to 1.0.1 ([#196](https://github.com/natanayalo/code-agent/pull/196))

- Chore(deps): bump react-router and react-router-dom in /dashboard ([#195](https://github.com/natanayalo/code-agent/pull/195))

- Chore(deps): bump idna from 3.11 to 3.15 ([#194](https://github.com/natanayalo/code-agent/pull/194))

- Chore(deps): bump uvicorn from 0.46.0 to 0.49.0 ([#193](https://github.com/natanayalo/code-agent/pull/193))

- Chore(deps): bump openai from 2.36.0 to 2.41.0 ([#192](https://github.com/natanayalo/code-agent/pull/192))

- Chore(deps): bump langchain-core from 1.3.3 to 1.4.1 ([#191](https://github.com/natanayalo/code-agent/pull/191))

- Chore(deps): bump langgraph-checkpoint from 4.0.3 to 4.1.0 ([#190](https://github.com/natanayalo/code-agent/pull/190))

- Chore(deps): bump langsmith from 0.7.32 to 0.8.0 ([#189](https://github.com/natanayalo/code-agent/pull/189))

- Chore(deps): bump urllib3 from 2.6.3 to 2.7.0 ([#187](https://github.com/natanayalo/code-agent/pull/187))

- Chore(deps): bump openai from 2.33.0 to 2.36.0 ([#186](https://github.com/natanayalo/code-agent/pull/186))

- Chore(deps): bump langchain-core from 1.3.2 to 1.3.3 ([#184](https://github.com/natanayalo/code-agent/pull/184))

- Chore(deps-dev): bump @babel/plugin-transform-modules-systemjs from 7.29.0 to 7.29.4 in /dashboard ([#183](https://github.com/natanayalo/code-agent/pull/183))

- Chore(deps-dev): bump fast-uri from 3.1.0 to 3.1.2 in /dashboard ([#181](https://github.com/natanayalo/code-agent/pull/181))

- Chore(deps): bump mako from 1.3.11 to 1.3.12 ([#178](https://github.com/natanayalo/code-agent/pull/178))

- Chore(deps): bump openai from 2.32.0 to 2.33.0 ([#151](https://github.com/natanayalo/code-agent/pull/151))

- Chore(deps): bump langgraph from 1.1.9 to 1.1.10 ([#150](https://github.com/natanayalo/code-agent/pull/150))

- Chore(deps): bump langgraph-checkpoint from 4.0.2 to 4.0.3 ([#149](https://github.com/natanayalo/code-agent/pull/149))

- Chore(deps): bump psycopg from 3.3.3 to 3.3.4 ([#148](https://github.com/natanayalo/code-agent/pull/148))

- Chore(deps-dev): bump pre-commit from 4.5.1 to 4.6.0 ([#130](https://github.com/natanayalo/code-agent/pull/130))

- Chore(deps): bump langgraph from 1.1.8 to 1.1.9 ([#129](https://github.com/natanayalo/code-agent/pull/129))

- Chore(deps): bump langchain-core from 1.3.0 to 1.3.2 ([#128](https://github.com/natanayalo/code-agent/pull/128))

- Chore(deps): bump fastapi from 0.136.0 to 0.136.1 ([#127](https://github.com/natanayalo/code-agent/pull/127))

- Chore(deps): bump uvicorn from 0.44.0 to 0.46.0 ([#126](https://github.com/natanayalo/code-agent/pull/126))

- Chore(deps): bump actions/setup-node from 4 to 6 ([#125](https://github.com/natanayalo/code-agent/pull/125))

- Chore(deps): bump actions/checkout from 4 to 6 ([#124](https://github.com/natanayalo/code-agent/pull/124))

- Chore(deps): bump esbuild, vite and vite-plugin-pwa in /dashboard ([#115](https://github.com/natanayalo/code-agent/pull/115))

- Chore(deps): remediate open Dependabot alerts ([#87](https://github.com/natanayalo/code-agent/pull/87))

- Chore(deps): bump actions/cache from 4 to 5 ([#86](https://github.com/natanayalo/code-agent/pull/86))

- Chore(deps): bump langgraph-checkpoint from 3.0.1 to 4.0.0 ([#85](https://github.com/natanayalo/code-agent/pull/85))

- Chore(deps): bump langchain-core from 0.3.76 to 1.2.28 ([#84](https://github.com/natanayalo/code-agent/pull/84))

- Chore(deps-dev): update pytest requirement from <10.0,>=8.4 to >=9.0.3,<10.0 ([#69](https://github.com/natanayalo/code-agent/pull/69))

- Chore(deps-dev): update pyyaml requirement from <7.0,>=6.0 to >=6.0.3,<7.0 ([#68](https://github.com/natanayalo/code-agent/pull/68))

- Chore(deps-dev): update alembic requirement from <2.0,>=1.16 to >=1.18.4,<2.0 ([#67](https://github.com/natanayalo/code-agent/pull/67))

- Chore(deps-dev): update psycopg requirement from <4.0,>=3.2 to >=3.3.3,<4.0 ([#66](https://github.com/natanayalo/code-agent/pull/66))

- Chore(deps): update langgraph-checkpoint-sqlite requirement from <4.0,>=3.0 to >=3.0.3,<4.0 ([#65](https://github.com/natanayalo/code-agent/pull/65))

- Chore(deps): bump actions/upload-artifact from 4 to 7 ([#44](https://github.com/natanayalo/code-agent/pull/44))

- Chore(deps-dev): update pytest requirement from <9.0,>=8.4 to >=8.4,<10.0 ([#9](https://github.com/natanayalo/code-agent/pull/9))

- Chore(deps): bump actions/checkout from 4 to 6 ([#8](https://github.com/natanayalo/code-agent/pull/8))

- Chore(deps): bump actions/setup-python from 5 to 6 ([#7](https://github.com/natanayalo/code-agent/pull/7))


### Documentation

- Docs: mark M20.4 Worker Supervisor v1 as completed ([#286](https://github.com/natanayalo/code-agent/pull/286))

- Docs: remove completed milestones from roadmap ([#261](https://github.com/natanayalo/code-agent/pull/261))

- Docs: update e2e QA and operator docs for Antigravity migration ([#256](https://github.com/natanayalo/code-agent/pull/256))

- Docs: add generated changelog workflow ([#238](https://github.com/natanayalo/code-agent/pull/238))

- Docs: mark T-188 as done ([#225](https://github.com/natanayalo/code-agent/pull/225))

- Docs: prepare bounded-scout lane planning for Milestone 18 ([#221](https://github.com/natanayalo/code-agent/pull/221))

- Docs: explicitly mark Phase 1 milestones as completed in roadmap ([#218](https://github.com/natanayalo/code-agent/pull/218))

- Docs: add Milestone 17.5 e2e stabilization plan ([#171](https://github.com/natanayalo/code-agent/pull/171))

- Docs: synchronize milestone numbering and update folder ownership ([#131](https://github.com/natanayalo/code-agent/pull/131))

- Docs: milestone A docs refresh and roadmap realignment ([#113](https://github.com/natanayalo/code-agent/pull/113))

- Docs: plan review fixes, priority reorder, and new tasks ([#54](https://github.com/natanayalo/code-agent/pull/54))

- Docs: refresh status, readme, and agents policy for Milestone 8 ([#41](https://github.com/natanayalo/code-agent/pull/41))

- Docs: update CLI-first implementation roadmap ([#30](https://github.com/natanayalo/code-agent/pull/30))

- Docs: update planning and status docs ([#27](https://github.com/natanayalo/code-agent/pull/27))

- Docs: update backlog and status with reference analysis findings ([#24](https://github.com/natanayalo/code-agent/pull/24))

- Docs: refine implementation plan critical path ([#21](https://github.com/natanayalo/code-agent/pull/21))

- Docs: add agent development workflows and skills ([#10](https://github.com/natanayalo/code-agent/pull/10))


### Fixed

- Fix: short-circuit verify_result and review_result nodes for scout tasks ([#267](https://github.com/natanayalo/code-agent/pull/267))

- Fix: E2E Forensic Investigation & Runtime Hardening (T-178) + Phase 2 Docs Refresh ([#217](https://github.com/natanayalo/code-agent/pull/217))

- Fix: rename repo_trusted to repo_approved to resolve CodeQL alert 6 ([#203](https://github.com/natanayalo/code-agent/pull/203))

- Fix: harden interaction response state machine semantics ([#174](https://github.com/natanayalo/code-agent/pull/174))

- Fix: stabilize gemini cli adapter and independent review persistence ([#106](https://github.com/natanayalo/code-agent/pull/106))

- Fix(ci): add explicit permissions to pip-audit workflow ([#59](https://github.com/natanayalo/code-agent/pull/59))

- Fix(api): revalidate callback URL on progress delivery ([#58](https://github.com/natanayalo/code-agent/pull/58))

- Fix: tighten worker prompt safety guidance ([#29](https://github.com/natanayalo/code-agent/pull/29))

- Fix: align orchestrator route and dispatch state ([#25](https://github.com/natanayalo/code-agent/pull/25))

- Fix(sandbox): map docker user to host and handle execution errors ([#18](https://github.com/natanayalo/code-agent/pull/18))

- Fix: docs ([#2](https://github.com/natanayalo/code-agent/pull/2))


### Maintenance

- Chore: harden CLI env secret scoping and align backlog docs ([#76](https://github.com/natanayalo/code-agent/pull/76))

- Chore: harden ci validation gates ([#19](https://github.com/natanayalo/code-agent/pull/19))

- Chore: add repo quality hooks and CI ([#4](https://github.com/natanayalo/code-agent/pull/4))

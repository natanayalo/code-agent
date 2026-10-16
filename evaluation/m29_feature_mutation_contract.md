# M29 `feature/mutation` Evaluation Contract

**Status:** operator-approved. The post-terminal execution-preparation mode is
implemented and smoke-verified; the runner and live collection remain gated on
the remaining preflight requirements below. This document defines the
evaluation only. It does not start live cases or change production routing.

## Purpose and evidence boundary

Compare the current Codex and Antigravity native mutable profiles on a controlled
set of feature tasks. The only repository used for a future run is a dedicated,
disposable fixture repository with no production code, customer data, or
credentials. Each provider receives the same ten prompts from the same clean
fixture commit, in an isolated workspace. Changes are delivered as workspace
artifacts and are never pushed or opened as pull requests.

## Terminal outcome and external quality boundary

The ordinary Temporal path runs verification and review before finalizing the
task. Verification can request bounded repairs, unresolved review findings can
set `manual_follow_up`, and Temporal returns that outcome as failed. That path
cannot support a claim that canonical completion is independent of those
quality checks.

Before the first case, the execution-preparation slice must provide and smoke
verify an evaluation mode that records the canonical task terminal state before
any runner-owned acceptance test, regression test, or model-based quality
evaluation runs. Those external checks run read-only against a frozen terminal
snapshot and write only to the evaluation bundle. They must not add task
timeline events or worker-run verification/review artifacts, request repairs or
manual follow-up, signal or replay a task, or change its persisted status,
terminal event, or diff. Preserve operational and sandbox safety controls.
If the current execution path cannot guarantee this separation, do not dispatch
cases until the execution slice implements and verifies the required mode.

After every case reaches terminal state, first archive a checksum-bound
snapshot of its task status, terminal event, and final diff. Then run the fixed
acceptance oracle, fixture regression suite, and independent review in an
isolated read-only evaluator. Verify after evaluation that the task status,
terminal event, and archived diff are unchanged. Any attempted post-terminal
mutation is a suite-stopping safety failure.

The fixture repository and its exact 40-character commit SHA are deliberately
left for the execution-preparation slice. Before any case starts, that slice
must freeze both in the suite manifest; a moving branch or tag is not a valid
revision. Each of the 20 cases starts from a fresh workspace at that exact
revision. No case may inherit another case's files or git state.

The evidence window remains the canonical 90-day window, with at least 10
eligible terminal tasks in each provider cell. A terminal failure with verified
identity is still an extractor observation. A cancelled, incomplete, identity
unknown, identity-mismatched, or otherwise excluded task does not count toward
the floor. Do not replay or replace a recorded case to fill a shortfall; report
the cell as insufficient data or prepare a separately versioned suite.

## Canonical cohort provenance

The canonical provider-reliability and robustness extractors consume every
eligible `feature/mutation` observation in the selected 90-day database window;
they do not limit the cells to this suite's cases. Therefore, before case 01,
freeze the exact eligible task-ID set for each canonical profile cell:
`codex-native-executor` and `antigravity-native-executor`. Run the canonical
extractor with `evidence_scope == "current_execution_cohort"`,
`task_class == "feature"`, `mutation_mode == "mutation"`, the frozen expected
execution identities, and the same 90-day window and report policy that will be
used for publication. Require `window_end_at == as_of` and freeze
`window_start_at == as_of - 90 days`. Freeze `as_of` and both window boundaries
in the private suite manifest, choosing an end time that includes the planned
suite completion, so baseline tasks cannot age out between baseline and report
generation. If collection cannot finish inside that window, stop and prepare a
new reviewed manifest; do not move the cutoff silently.

The `evaluation.m29_mutation_cohort.reconcile_m29_mutation_cohort` gate checks
the post-cutoff inputs before report publication: every scheduled case must
have a terminal timestamp at or before `as_of`, every case must be classified
exactly once as eligible or excluded, the frozen baseline must be disjoint from
the suite and match its frozen SHA-256, and each canonical extractor/report
snapshot must equal the frozen baseline plus eligible suite IDs. Its returned
public-safe record contains counts and hashes only. The caller must supply the
canonical database snapshot; this helper does not launch cases or query the
database.

Store each baseline as a sorted exact ID set in the access-controlled private
bundle, together with the extractor/build and policy hashes, profile, window
boundaries, count, and SHA-256 of the sorted IDs using a frozen canonical
encoding. Keep the raw IDs private. For each provider, classify every scheduled
case task with that same extractor and policy as either eligible or excluded
with its exact exclusion reason. Missing, duplicated, or unclassified suite
tasks block report publication. Define `eligible_suite_ids[profile]` as the IDs
of this suite's eligible case tasks.

The frozen `as_of` is the cohort's upper timestamp boundary, not permission to
reconcile or publish early. The run plan must reserve worst-case task execution
and terminalization time for every planned case. Do not dispatch a case unless
the remaining schedule ensures every suite task can reach terminal state at or
before `as_of`; stop starting cases early enough to preserve that bound. If any
suite task terminalizes after `as_of`, or the suite misses the cutoff, do not
perform final reconciliation or publish reports. Preserve terminal outcomes
and prepare a newly reviewed, versioned manifest; do not move the existing
cutoff.

Wait until the frozen `as_of`/`window_end_at` has elapsed (current UTC time is
at or after it) before final extraction, exact-set reconciliation, or report
generation. After that point, rerun the canonical extractor against the
report-source database and require, for each profile, exact set equality:

```text
current_cell_ids[profile] == frozen_baseline_ids[profile] ∪ eligible_suite_ids[profile]
```

Generate the reliability and robustness reports from that same post-cutoff
database snapshot, or verify exact equality against each report's own extractor
snapshot before publishing it. Never publish from a pre-cutoff extraction or
reconciliation.

The baseline and suite ID sets must be disjoint. Any additional or missing ID,
including an unrelated eligible task, a baseline task that disappeared, or an
eligible suite task omitted by extraction, fails closed and blocks publication
of cell counts, accepted counts, Wilson bounds, latency statistics,
recommendations, and robustness results. Do not silently rebaseline, drop an
observation, or add it to the suite. Publication may resume only after every
difference is recorded by private task ID with its profile, disposition,
reason, and supporting evidence in a separately reviewed, versioned
reconciliation, and a rerun of the exact-set assertion passes. If that requires
changing the expected cohort, review and version that change before report
generation; never silently adopt the observed IDs. The sanitized/public artifact
may expose baseline, suite, and current counts; hashes of the sorted ID sets;
and reconciliation status, but never raw task IDs.

## Frozen case pack

The execution suite contains ten task topics, each run once per provider: 20
cases total and ten matched pairs. Within each pair, the prompt and external
acceptance oracle below are byte-for-byte identical. Run the cases sequentially
in pair order. The first provider alternates by pair to counterbalance ordering:

| Pair | First | Second |
| --- | --- | --- |
| 01 | Codex | Antigravity |
| 02 | Antigravity | Codex |
| 03 | Codex | Antigravity |
| 04 | Antigravity | Codex |
| 05 | Codex | Antigravity |
| 06 | Antigravity | Codex |
| 07 | Codex | Antigravity |
| 08 | Antigravity | Codex |
| 09 | Codex | Antigravity |
| 10 | Antigravity | Codex |

Use the mutable profiles `codex-native-executor` and
`antigravity-native-executor`. At contract review time, the expected execution
identities in the canonical report policy are Codex `gpt-6-luna/high` and
Antigravity `gemini-3.8-flash/medium`. Freeze the expected provider, model, and
reasoning effort in the run manifest before case 01. If the supported identity
has changed before collection, revise and review the manifest before any case;
never mix model vintages within one run.

The fixture baseline must provide a small Python 3.12+ `taskboard` package with
the named modules, function signatures, and data types below. `Task` has `id`,
`title`, `status`, optional `owner`, integer `priority`, `list[str]` tags, and
an aware `created_at` datetime. `Event` has `id`, `event_type`, and a payload.
`Page` exposes `items` and `next_cursor`. The ordinary regression suite must
pass at the pinned baseline; each case adds one bounded behavior covered by the
external acceptance oracle. Keep the fixture standard-library-only and free of
network, file system, database, and secret dependencies. Put fixed acceptance
tests in a runner-owned, read-only verifier package outside the writable
workspace. The provider cannot edit, skip, or replace that verifier. Run its
checks against a read-only snapshot after the task reaches terminal state, not
as a task completion gate.
The module named for a pair is its only allowed source path. Test files,
verifier files, baseline metadata, dependency files, and repository profile are
protected. If a shared helper path is needed, add it to the reviewed suite
allowlist before any case starts.

For every prompt, append this same instruction verbatim:

> Make the smallest implementation for this request in the task workspace. Do
> not change unrelated files or add dependencies. Report the files changed and
> any blockers. Do not modify or invoke runner-owned evaluation checks.

### Pair 01 — label normalization

**Module/API:** `taskboard/labels.py`,
`normalize_label(value: str) -> str`.

**Prompt:** Implement `normalize_label` so it applies Unicode NFKC
normalization and case folding, replaces each run of non-alphanumeric
characters with one hyphen, and removes leading and trailing hyphens.

**Acceptance oracle:** `"  Quick   Fix! "` becomes `"quick-fix"`;
`"café & TEA"` becomes `"café-tea"`; full-width `"ＦＯＯ"` becomes `"foo"`;
punctuation-only input becomes the empty string. The input is not mutated.

### Pair 02 — deterministic task ordering

**Module/API:** `taskboard/ordering.py`, `sort_tasks(tasks: list[Task]) ->
list[Task]`.

**Prompt:** Implement `sort_tasks` to order tasks by descending priority, then
ascending `created_at`, then ascending task `id` for ties. Return a new list and
leave the input order unchanged.

**Acceptance oracle:** For input IDs `["b", "a", "c", "d"]`, where `b` and
`a` have priority 2 and the same timestamp, `c` has priority 3 and a later
timestamp, and `d` has priority 2 and an earlier timestamp, return IDs
`["c", "d", "a", "b"]`. Verify the input remains `["b", "a", "c", "d"]`.

### Pair 03 — combined task filters

**Module/API:** `taskboard/filters.py`,
`filter_tasks(tasks, *, status=None, owner=None, tag=None) -> list[Task]`.

**Prompt:** Implement optional task filters for exact status, case-insensitive
owner, and exact tag. When more than one filter is supplied, require all of them
to match. Preserve source order and leave the input unchanged.

**Acceptance oracle:** Use tasks `a=(queued, Sam, [red])`, `b=(done, sam,
[red])`, `c=(queued, Lee, [red, blue])`, and `d=(in_progress, SAM, [blue])`,
in that order. Status `queued` returns `[a, c]`; owner `SAM` returns `[a, b,
d]`; tag `red` returns `[a, b, c]`; all three filters set to
`queued`/`sAm`/`red` return `[a]`; `done`/`Lee` returns `[]`; no filters returns
all four in their original order.

### Pair 04 — cursor pagination

**Module/API:** `taskboard/pagination.py`,
`paginate_tasks(tasks, *, limit: int, after_id: str | None = None) -> Page`.

**Prompt:** Implement exclusive cursor pagination over the supplied task order.
The cursor is the ID of the last item on the previous page. Return a `Page` with
the page items and a `next_cursor` set to the last returned ID only when more
items remain.

**Acceptance oracle:** Cover first, middle, final, empty, and single-page
requests over IDs `[a, b, c, d]`: limit 2 returns `[a, b]` with cursor `b`,
then after `b` returns `[c, d]` with no cursor; limit 4 returns all items with
no cursor; after `d` returns an empty page. A non-positive limit or unknown
cursor raises `ValueError`; the cursor item is not repeated.

### Pair 05 — CSV export

**Module/API:** `taskboard/export.py`,
`export_tasks_csv(tasks: list[Task]) -> str`.

**Prompt:** Implement an in-memory CSV export with the fixed header
`id,title,status,owner,priority`, preserving task order and emitting an empty
field for a missing owner. Use standard CSV escaping and LF line endings.

**Acceptance oracle:** Verify the exact header and row order. A title containing
a comma round-trips unchanged; a title containing a quote and newline
round-trips unchanged; a missing owner parses as an empty field. Assert all
record separators are LF and the function performs no file I/O.

### Pair 06 — status transitions

**Module/API:** `taskboard/status.py`,
`transition_status(current: str, requested: str) -> str`.

**Prompt:** Implement the task status transition rules: `queued` may move to
`in_progress` or `cancelled`; `in_progress` may move to `done` or `cancelled`;
`done` and `cancelled` are terminal. A request for the current known status is
an idempotent no-op.

**Acceptance oracle:** Verify every allowed edge, same-state no-op for each
known status, and rejection of every other or unknown state with `ValueError`.

### Pair 07 — capped retry delay

**Module/API:** `taskboard/retry.py`,
`retry_delay_seconds(attempt: int, *, base_seconds: int = 1, max_seconds: int =
30) -> int`.

**Prompt:** Implement deterministic exponential retry delay as
`min(max_seconds, base_seconds * 2 ** (attempt - 1))`. Attempts start at one;
base and maximum delays may be zero but cannot be negative. Return the maximum
delay without constructing an unbounded integer for a very large attempt.

**Acceptance oracle:** With defaults, attempts 1, 2, 3, and 6 return 1, 2, 4,
and 30. Verify a custom base and cap, zero delay, a very large attempt, and
`ValueError` for an attempt below one or a negative delay.

### Pair 08 — event deduplication

**Module/API:** `taskboard/events.py`,
`deduplicate_events(events: list[Event]) -> list[Event]`.

**Prompt:** Implement event deduplication by event ID. Keep the first occurrence
of each ID, preserve the order of those first occurrences, and return a new list
without modifying the input.

**Acceptance oracle:** Verify repeated IDs with different payloads keep the
first event, unique events keep their order, empty input returns empty, and the
input list is unchanged.

### Pair 09 — event counts

**Module/API:** `taskboard/events.py`,
`count_events_by_type(events: list[Event]) -> dict[str, int]`.

**Prompt:** Implement event counts grouped by the exact `event_type` string.
Return an empty mapping for no events and a deterministic mapping for non-empty
input.

**Acceptance oracle:** For types `["create", "update", "create", "Create"]`,
return `{"Create": 1, "create": 2, "update": 1}` with keys in lexical order.
Empty input returns `{}` and the event list is unchanged.

### Pair 10 — task status summary

**Module/API:** `taskboard/summary.py`,
`summarize_tasks(tasks: list[Task]) -> dict[str, int]`.

**Prompt:** Implement `summarize_tasks` to return counts for `total`, `queued`,
`in_progress`, `done`, `cancelled`, and `other`. Count every task in `total`,
count known statuses in their own field, and count unrecognized statuses in
`other`. Return all six keys even when their counts are zero.

**Acceptance oracle:** For statuses `["queued", "done", "done", "cancelled",
"archived"]`, return `{"total": 5, "queued": 1, "in_progress": 0, "done": 2,
"cancelled": 1, "other": 1}`. Empty input returns all six keys with zero
values; the task list is unchanged.

## Preflight and persisted TaskSpec gate

Do not launch the suite until all of these checks pass:

- The repository is dedicated and disposable, its remote contains no valuable
  data, and the pinned commit is readable and clean. The verifier and suite
  hashes, repository revision, build SHA, candidate and evaluator identities,
  evaluator prompt/tool hashes and no-fallback policy, memory baseline hash,
  case order, budgets, evaluation mode, and retention configuration are frozen
  in a new private evidence bundle.
- The disposable Postgres, Temporal, and sandbox stack is isolated from
  production. Provider egress is limited to the approved provider endpoints and
  read-only fixture fetch. No GitHub write token, repository write credential,
  production secret, or unrelated secret reference is available to a case.
- Freeze the exact candidate and evaluator identities in the suite manifest.
  The model-based verifier and independent reviewer use the same neutral
  third-party provider, model, and reasoning effort for all 20 cases; this
  evaluator must differ from both candidate providers. Pin its system-prompt
  hashes, read-only tool set, runtime, and equal per-case budgets. Set
  `fallback_policy` to `none`; never use the normal verifier/reviewer fallback
  chains or fall back to either candidate or another model. The deterministic
  acceptance and regression checks have no model identity; pin their verifier
  source hash, interpreter, and environment.
  If the evaluator is unavailable or its actual identity differs, stop the
  suite without switching evaluators. Freeze candidate identity as specified
  above and verify actual identities from trusted execution metadata.
- Freeze the complete worker-visible memory baseline. Personal memory is
  operator-global: require the global `PersonalMemory` store to be empty, or
  run against a dedicated disposable database/schema with no access to the
  operator store. The current `_fetch_memory_context` path does not scope its
  `PersonalMemoryRepository` query by `user_id`. Also freeze project memory for
  the fixture repository URL,
  a fresh empty session/thread, observations, repository profile, and any other
  field in `MemoryContext`. Before every dispatch, hash the canonical serialized
  `MemoryContext` after loading, read-side gates, profile shaping, and context
  assembly; compare it with the frozen baseline hash. Include the session,
  observations, repository profile, and final worker-visible memory payload in
  the hash. After each case, isolate or discard memory writes before the next
  case. Per-case databases/namespaces are acceptable only if they reproduce the
  same frozen baseline. Record hashes, not memory contents. Stop if isolation
  or equality cannot be proved.
- Each case uses a new isolated workspace cloned from the pinned revision. The
  fixture has no profile delivery default other than `workspace`; that setting
  does not replace the persisted-state check below.
- Before dispatch, read the finalized TaskSpec from persisted task state after
  repository-profile processing. Require `task_type == "feature"`,
  `"modify_workspace_files"` in `allowed_actions`, `delivery_mode ==
  "workspace"`, no `delivery_branch`, no branch/draft-PR delivery actions,
  `requires_clarification == false`, empty `clarification_questions`, and
  `requires_permission == false`.
  Require empty `verification_commands` after repo-profile processing; the
  fixture repo profile's `quick` and `full` validation lists must be empty for
  the task workflow. The runner-owned acceptance and regression commands are
  frozen separately and run only after terminalization. The evaluation mode
  must require `enable_independent_verifier == false` and the in-task
  `skip_independent_review` constraint. It must also prevent deterministic
  verification or worker-reported test results from requesting a quality repair
  or changing terminal status. A preflight smoke must prove that pass, fail, and
  unavailable post-terminal evaluations leave the task status and terminal
  event unchanged. For each pass, fail, and unavailable smoke variant, and for
  each candidate profile, also run the actual canonical provider-reliability
  extractor against representative persisted records in a separate disposable
  smoke database/schema that is never used for the frozen baseline or suite
  report. Use the same extraction policy and eligible task shape as the planned
  suite; exercise the worker-reported-failed-tests short-circuit path as well as
  the ordinary no-verification-command path. Inspect the extractor's per-task
  evidence and assert `verification_applicable == false` and
  `review_applicable == false` for every smoke record, and assert the canonical
  report's verification and review stage rates are both `None` (not applicable).
  Confirm there is no
  `ArtifactType.INDEPENDENT_REVIEW_RESULT` in any smoke task's worker-run
  artifact index. Also confirm status and terminal event remain unchanged for
  all three external-evaluation outcomes. If any assertion fails or any smoke
  record is not classified by the extractor as expected, do not dispatch cases
  until the execution slice implements and verifies the mode.
  Confirm the task has the expected provider profile and the fixture revision.
  Stop before provider dispatch if any field differs. Do not repair the
  persisted TaskSpec in place and continue.
- For each matched pair, require identical prompt and acceptance-oracle hashes
  and compare the evaluation-controlled semantic fields: persisted post-overlay
  TaskSpec, constraints, budget, fixture revision, repository profile, task
  prompt, acceptance-oracle hash, and evaluator manifest. Goal, acceptance
  criteria, non-goals, actions, setup commands, empty task-workflow verification
  commands, workspace mode, delivery mode, and budgets must match. Provider
  profile and actual candidate identity are the treatment. Operational values
  may differ and must not be compared for equality: task/case/session/thread and
  workspace IDs or paths; run, attempt, and artifact IDs; timestamps; and
  trace/span IDs. Stop before either member is dispatched if any controlled
  semantic field differs.
- Freeze equal per-case limits for both members of every pair: a 900-second
  candidate execution timeout, identical task budget fields, and identical
  evaluator time/token/cost budgets. External verifier and review repair counts
  are always zero; record task execution attempts separately. Do not retry a
  terminal task on another provider.
- Before case 01, verify the evaluation service's retention and cleanup sweep
  enforce the 72-hour maximum below, including when the case runner stops. Any
  automatic cleanup must use the recorded workspace ownership and
  `WorkspaceManager` root boundary; otherwise configure the independent manager
  watchdog and prevent another retention path from deleting evaluation
  workspaces before evidence archival.
- Run the baseline fixture checks and read-only verifier integrity check before
  the first case. Confirm no evaluation workspace or output path overlaps the
  primary checkout or another task workspace.

The TaskSpec check is mandatory because `apply_repo_profile_to_task_spec` may
replace a requested `workspace` delivery mode with the fixture profile's
delivery default. Re-read persisted state immediately before dispatch; a
pre-overlay request or generated in-memory TaskSpec is not sufficient evidence.
Gate any later transition to a delivery stage on the same persisted value. If
delivery mode changes or delivery metadata/events appear, block further delivery
work and stop the suite before another case is dispatched.

## Per-case evidence and outcome semantics

Keep an access-controlled private bundle with an immutable terminal record and
a separate post-terminal quality record for every case. Capture at least:

- suite, pair, and case IDs; build SHA; pinned fixture revision; persisted final
  TaskSpec fields relevant to classification and delivery; selected profile;
- actual candidate provider, model, and reasoning effort from trusted
  `budget_usage.native_agent.model_execution` metadata for every task worker run.
  Do not infer identity from the profile name or environment. All candidate runs
  in a case must agree;
- the actual post-terminal evaluator provider, model, and reasoning effort for
  every model-based verifier and reviewer call. Compare each with the frozen
  neutral evaluator identity; do not infer identity from a configured profile;
- immutable task status, terminal timeline event and timestamp captured before
  quality evaluation, execution/runtime mode, and extractor eligibility or
  exclusion reason;
- checksum of the terminal snapshot, final workspace diff, changed relative
  paths, and a clean/dirty status check;
- external deterministic acceptance and regression results, verifier source
  hash, commands, exit codes, runtime/toolchain, and independent reviewer
  outcome/findings from the separate evaluation record;
- in-task execution attempts and repair counts, clarification/approval/other
  interaction counts, typed task failure kind, zero external evaluator repairs,
  and configured/reported wall-time, token, and cost budgets;
- workspace ID, task ID, workspace path, trusted Git directory, cleanup-policy
  state, retention deadline, and cleanup result for exact ownership accounting.

Run the fixed acceptance oracle and fixture regression suite on a read-only
snapshot after task terminalization. Run every model-based evaluation role
(including any independent verifier and reviewer) through the same neutral
third-party provider/model/reasoning-effort identity frozen in the manifest,
with the frozen prompt hashes and read-only tools. The reviewer receives the
same acceptance criteria but not candidate provider identity or case order.
Model-based evaluation is read-only and has no repair pass. Store all external
quality records in the evaluation bundle, not the task timeline or worker-run
artifact index. Do not emit `VERIFICATION_COMPLETED` events or
`ArtifactType.INDEPENDENT_REVIEW_RESULT` artifacts for these external results.
The canonical extractor's in-task verification/review stage rates therefore
remain not applicable; publish external quality measures separately instead of
backfilling those extractor stages.

After the terminal snapshot is archived, no quality outcome may signal, replay,
repair, cancel, or otherwise mutate the task. Re-read task status and terminal
timeline after external evaluation and compare them with the snapshot. A
quality-failing completed task remains `COMPLETED`; a failed task retains its
terminal failure even when its partial diff passes external checks. A nonzero
external repair count or a changed task status, terminal event, or archived diff
is a suite-stopping contract violation. A missing typed task failure remains
`unknown`; do not guess one. Keep task IDs, raw provider logs, and artifact URIs
in the access-controlled raw bundle. Also write a separate sanitized review
archive with case ID, actual candidate and evaluator identities, terminal and
quality outcomes, sanitized final diff, fixture-relative changed paths,
external verification and review results, repairs, interactions, failure kind,
and budgets. Scan the diff and captured fields for credentials before archiving;
do not put task IDs, workspace IDs, repository URLs, raw logs, or artifact URIs
in the sanitized archive. Verify its checksum and case coverage before cleanup.
Public M29 reports remain more restrictive: follow the existing allowlist and
omit task text, raw diffs, raw logs, repository URLs, credentials, and private
artifacts.

Report two distinct results:

1. **Pre-evaluation task completion:** use the canonical extractor's accepted
   outcome from the immutable terminal snapshot: persisted `COMPLETED` plus a
   consistent terminal timeline. `FAILED` is non-accepted and retains its typed
   failure. `CANCELLED` and nonterminal/interrupted tasks remain excluded under
   current extractor semantics. This measures whether the task/orchestration
   path terminalized before external quality evaluation; it does not measure
   feature correctness. External quality outcomes cannot change it, though task
   completion may still correlate with implementation quality.
2. **Independent feature quality:** pass only when the fixed external
   acceptance checks pass, the fixture regression suite passes, and the blinded
   neutral reviewer records no blocking finding on the terminal diff. Record
   `fail` or `not assessable` otherwise, with the reason. A completed task may
   fail this quality check; a failed task may still have a reviewable partial
   diff. Never fold this result into canonical task status or the extractor's
   in-task verification/review stages.

Use the current extractor to classify each task as `feature/mutation`: the
persisted TaskSpec must retain the feature task type and include
`modify_workspace_files` in `allowed_actions`. The exact-profile, actual-model,
and identity checks remain fail-closed. The report's 90-day window and minimum
of ten eligible tasks per provider cell still apply; the 20 scheduled attempts
do not guarantee eligibility if cases are excluded. Before publication, apply
the exact baseline-plus-suite task-ID reconciliation above to both provider
cells and to the observation snapshot consumed by the robustness analysis.

## Stop conditions

Stop dispatching new cases immediately if any of these occurs:

- the pre-dispatch persisted TaskSpec or pinned fixture revision check fails;
- the full worker-visible `MemoryContext` or memory payload hash differs from
  the frozen baseline, or prior-case memory is visible to the current case;
- actual candidate or evaluator identity is missing, mixed across runs, or
  differs from the frozen provider/model/reasoning-effort identity; preserve any
  terminal task outcome, mark external quality not assessable when applicable,
  and stop before dispatching later cases;
- the evaluator is unavailable or attempts a fallback. Do not switch models;
  preserve existing terminal outcomes, mark pending quality assessments not
  assessable, and stop further case dispatch;
- a verification/review repair or manual-follow-up transition occurs before
  terminalization, showing that evaluation quality gates were not isolated;
- any post-terminal evaluator changes task status, the terminal event, or the
  archived diff;
- a case pauses for clarification, approval, or permission, or creates a pending
  human interaction. Stop dispatching immediately; preserve the pending task,
  interaction, and timeline state. Do not answer, approve, reject, cancel, or
  replay it automatically. Record the case as interaction-pending and leave it
  unresolved for an operator; no later case may start in this suite;
- a task requests or reaches branch/draft-PR delivery, produces delivery
  metadata/events, pushes a ref, creates a pull request, or changes any remote
  state;
- a workspace escapes its designated root, overlaps another workspace, or
  accesses a non-allowlisted destination or credential;
- the task modifies the fixture verifier, suite, baseline metadata, dependency
  declarations, or any other protected path;
- the worker or stack reports a safety-policy violation or an unexplained
  change outside the case's allowed source paths.

Persist any already-terminal outcomes exactly as observed; never rewrite,
reclassify, or replay them. Mark cases without terminal state as interrupted,
retain their partial artifacts, and exclude them under current extractor
semantics. Record the stop reason and elapsed position in the private bundle.
Unexpected remote effects require operator investigation under the applicable
repository procedure; do not try to delete remote refs or PRs as evaluation
cleanup.

A deterministic acceptance failure, regression failure, or review finding is a
quality result, not by itself a reason to stop dispatch or repair the task. It
must leave the already-frozen task outcome unchanged. Evaluator identity or
availability failures stop the suite as described above.

## Retention and cleanup

The worker cleanup policy retains successful native workspaces and failed
workspaces by default. Preserve every evaluation workspace until its sanitized
evidence record has been archived and verified. Retain failed, interrupted, and
quality-failing workspaces for at most 72 hours for bounded inspection; successful
quality-passing cases may be cleaned up as soon as their evidence is archived.
Do not extend retention automatically. The current `TaskExecutionService`
default retention is seven days and does not satisfy this contract. Before case
01, set the evaluation stack's `retention_seconds` to no more than 259,200
seconds and verify that expired runs are swept on schedule. If that service
cannot guarantee cleanup when the runner stops, install an independent watchdog
that calls `WorkspaceManager` for the exact recorded evaluation workspace IDs
by the 72-hour deadline. The expiry path must use the recorded-ID and
`WorkspaceManager` boundaries; if the built-in sweep deletes evaluation
workspaces outside those boundaries, disable that deletion for evaluation
handles and use the watchdog. A TTL without a guaranteed manager cleanup sweep
is insufficient. If evidence archival fails, report the archive failure; the
watchdog must still enforce the 72-hour maximum rather than extending retention.

After archival and any inspection window, remove only workspaces whose exact IDs
and resolved paths were recorded in the evaluation bundle. Set a one-time
cleanup-policy copy on the recorded handle (`delete_on_success=true`,
`retain_on_failure=false`) and call `WorkspaceManager.cleanup_workspace` with
that handle and the recorded case outcome. The manager evaluates the policy on
the handle and enforces its configured root boundary and cleanup of
workspace-owned scratch and trusted Git directories. If it refuses or fails,
record the error and stop; do not fall back to shell `rm`, broad root cleanup,
or an inferred workspace path. Do not delete the fixture repository, the
evidence bundle, or any unrecorded workspace. Verify each recorded workspace
and its manager-owned scratch/trusted Git directories are absent after
successful cleanup.

The preflight and closeout checklist applies to every outcome:

| Case outcome | Before cleanup |
| --- | --- |
| Completed, quality pass | Freeze terminal record and final diff before evaluation; archive external quality evidence; then clean the recorded workspace. |
| Completed, quality fail | Keep the terminal record unchanged; archive failed checks/review; retain for up to 72 hours for inspection; then clean the recorded workspace. |
| Quality not assessable | Keep the terminal record unchanged; archive the evaluator/infra failure; retain for up to 72 hours for inspection; then clean the recorded workspace. |
| Failed terminal task | Preserve terminal failure and typed failure kind; archive any assessable partial-diff quality result; retain for up to 72 hours for inspection; then clean the recorded workspace. |
| Interrupted or cancelled | Record interruption/cancellation without converting it to failure; archive partial evidence; retain for up to 72 hours for inspection; then clean the recorded workspace. |
| Interaction pending | Preserve the pending task and interaction without resolving it; archive sanitized evidence; retain the workspace for up to 72 hours for inspection; then clean it through the manager. |
| Preflight stopped before dispatch | Record the stop and whether a workspace was provisioned; archive the sanitized stop evidence; clean only a workspace recorded to that case. |
| Safety, identity, or delivery mismatch | Halt the suite; freeze any terminal records; archive sanitized evidence; retain affected workspace for up to 72 hours for inspection; then clean it through the manager. |

If any eligible count is below ten for either profile, publish the canonical
report as insufficient data for that cell. Evaluation results remain advisory;
production routing and `evaluation/routing_metrics.json` stay static.

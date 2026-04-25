# Worker Routing Policy

## Purpose

Choose the most appropriate coding worker for a task while keeping decisions explainable.

## Currently configured workers

- `CodexCliWorker`: Production-class worker using the `codex exec` CLI adapter and persistent shell sessions.
- `GeminiCliWorker`: Production-class worker using the Gemini CLI adapter and persistent shell sessions.

## Planned workers

None — both workers are available.

## Route to Gemini-family worker when

Use the Gemini-family worker if one or more are true:
- task is ambiguous
- task spans many files
- task is architecture-sensitive
- task is a refactor rather than a straightforward implementation
- prior attempt with CodexCliWorker failed
- prior verifier output suggests the cheaper worker under-scoped the task
- caller explicitly requests "highest quality"

If the requested Gemini-family runtime is not configured or not available in the current
environment, the orchestrator must fail explicitly rather than silently dispatching another
worker while state still claims `gemini`.

## Route to Codex-family worker when

Use the Codex-family worker when the task is straightforward and the following indicators apply:
- task is straightforward
- task is lower-risk
- task is repetitive or mechanical
- caller explicitly prefers lower cost
- change scope is small to medium
- repo context is already well understood
- runtime availability and budget preference favor the cheaper path

## Manual override

If task payload specifies `worker_override`, always honor it unless policy forbids it or the
requested runtime is unavailable.

## Escalation policy

If first worker fails:
1. inspect failure type
2. if failure is likely model/strategy-related, reroute to alternate worker
3. if verifier failure suggests the implementation was mis-scoped, retry only with an explicit route reason
4. if failure is environment-related, retry same worker after environment fix
5. never retry blindly more than configured limit

The orchestrator should branch on typed `failure_kind` / verifier `failure_kind` values
instead of parsing free-form summaries.

## Iteration stall handling policy

Treat "command executed successfully" and "task made progress" as separate signals.

### Stop reasons

When a run exhausts loop budget without converging, prefer typed stop reasons over a generic
`max_iterations` summary:
- `stalled_in_inspection`: repeated read-only behavior without convergence
- `exploration_exhausted`: exploration phase budget consumed before plan/edit/final answer
- `no_progress_before_budget`: turns were consumed but no meaningful task progress occurred

`max_iterations` remains a runtime guard, but final retry/reroute policy should branch on
the more specific stop reason.

### Progress heuristics

The runtime should evaluate iteration-level progress, not only exit codes. Strong stall signals:
- only read-only commands across the last `N` turns
- no file changes yet
- repeated reads of the same file(s)
- same file inspected more than `M` times
- no new files discovered in recent turns
- no short plan checkpoint produced
- no final-answer candidate produced

### Mid-run correction before failure

If stall signals cross threshold, inject one corrective runtime message before failing:

1. produce a concise plan,
2. make the first concrete change, or
3. return a final answer summarizing findings and what's missing.

If there is still no progress after 1-2 turns, stop as a stall-class reason instead of
continuing blind exploration.

### Phase-aware budgeting

For ambiguous tasks, treat loop budget as phased:
- exploration
- execution/synthesis

Example shape:
- `max_exploration_iterations`: 4
- `max_execution_iterations`: 6

If exploration budget is exhausted without a plan/edit/final answer checkpoint, force a
transition or stop with a typed stall reason.

### Retry strategy for stall-class failures

Do not retry with the same prompt and same strategy. For stall-class stop reasons:
- rerun with stronger "stop reading and synthesize" instruction
- optionally require a planning checkpoint early (for broad architecture/docs tasks)
- narrow scope or decompose task
- provide explicit file hints from prior attempt
- reroute only when failure pattern suggests model strategy mismatch

### Persist and reuse partial progress

On stall-class stop reasons, persist partial execution context for the next attempt:
- files inspected
- repeated files / repeated ranges
- short findings summary
- stop reason and trigger signals

Retry prompts should explicitly reuse this context and discourage redundant rereads unless needed.

## Route reason

Every route decision must save:
- selected worker
- reason code
- human-readable reason

Example reason codes:
- `high_stakes_refactor`
- `cheap_mechanical_change`
- `previous_worker_failed`
- `verifier_failed_previous_run`
- `manual_override`
- `ambiguous_task`
- `budget_preference`
- `preferred_unavailable` (heuristic preferred worker absent; fallback used, task proceeds)
- `runtime_unavailable` (requested worker absent and no viable fallback; dispatch will fail)

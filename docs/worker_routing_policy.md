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

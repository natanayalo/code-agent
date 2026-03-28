# Worker Routing Policy

## Purpose

Choose the most appropriate coding worker for a task while keeping decisions explainable.

## Default workers

- ClaudeWorker
- CodexWorker

## Route to ClaudeWorker when

Use ClaudeWorker if one or more are true:
- task is ambiguous
- task spans many files
- task is architecture-sensitive
- task is a refactor rather than a straightforward implementation
- prior attempt with CodexWorker failed
- caller explicitly requests "highest quality"

## Route to CodexWorker when

Use CodexWorker when the task is straightforward and the following indicators apply:
- task is straightforward
- task is lower-risk
- task is repetitive or mechanical
- caller explicitly prefers lower cost
- change scope is small to medium
- repo context is already well understood

## Manual override

If task payload specifies `worker_override`, always honor it unless policy forbids it.

## Escalation policy

If first worker fails:
1. inspect failure type
2. if failure is likely model/strategy-related, reroute to alternate worker
3. if failure is environment-related, retry same worker after environment fix
4. never retry blindly more than configured limit

## Route reason

Every route decision must save:
- selected worker
- reason code
- human-readable reason

Example reason codes:
- `high_stakes_refactor`
- `cheap_mechanical_change`
- `previous_worker_failed`
- `manual_override`
- `ambiguous_task`

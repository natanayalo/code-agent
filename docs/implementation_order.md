# Implementation Order

Follow this order unless explicitly instructed otherwise.

## Step 1
Bootstrap repo, local infra, health endpoint

## Step 2
DB schema and repository layer

## Step 3
LangGraph workflow skeleton with fake worker

## Step 4
Checkpoint persistence and approval interrupts

## Step 5
Sandbox workspace and artifact capture baseline (through T-032)

## Step 6
First real worker interface and implementation (T-040/T-041)

## Step 7
Architecture review checkpoint after T-041
- Verify real worker output matches the Worker interface contract.
- Verify orchestrator state schema holds under real execution paths.
- Document mismatches before expanding scope.

## Step 8
Persistent sandbox + prompt foundations (T-045/T-046)
- Keep the long-lived container/session layer stable as the execution substrate for iterative workers.
- Keep the structured system prompt stable, but separate stable scaffold content from dynamic task/run context so CLI adapters can reuse what they control externally.

## Step 9
Shared CLI worker runtime (T-047)
- Implement the first real multi-turn worker through a CLI/SDK/hook/subprocess adapter rather than assuming direct raw API ownership.
- Reuse the existing shared worker contract, persistent sandbox, and prompt module.
- Keep the worker safe to run standalone with max iterations, worker-local timeout, and budget guards.

## Step 10
Tool registry + permission ladder + runtime budget enforcement (T-048, T-049)
- Add an explicit tool registry starting with a single bash tool surface.
- Move approval enforcement from a coarse task-text gate toward the tool/command boundary.
- Make budget a real control surface before the vertical slice depends on it.

## Step 11
Outer timeout/cancel + Vertical Slice E2E (T-042, T-044)
- `T-042` adds the orchestrator-level timeout/cancel envelope around the real worker path.
- `T-044` proves the minimal HTTP submit -> orchestrator -> real CLI worker -> real workspace -> DB -> task_id/status flow.
- DB scope here is execution-path persistence only: task/status, worker run metadata, final result fields, verifier output, and captured artifacts needed for polling by `task_id`.

## Step 12
Verifier stage + sandbox auditability (T-055, T-054)
- Add a constrained verifier after the builder worker finishes.
- Expand sandbox audit artifacts so verification and replay are grounded in what actually happened.

## Step 13
Skeptical memory + compaction + stable session scaffold (T-060 to T-065)
- Load memory as hints, not truth, with verification metadata.
- Maintain compact session working state instead of transcript-shaped state growth.
- Persist stable scaffold/session context separately from dynamic turn state where the CLI runtime allows it.

## Step 14
Structured run observability and second worker routing (T-043, T-070+)

## Step 15
Telegram ingress and generic webhook adapters (T-050 to T-053) on top of the existing HTTP path once the core execution path is stable

## Step 16
External tool wrappers, MCP compatibility, and remaining hardening

## Rule

Do not implement step N+1 until step N has:
- passing tests
- usable logs
- stable local run path

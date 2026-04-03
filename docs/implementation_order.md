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
Persistent sandbox + prompt + self-bounded multi-turn worker (T-045 to T-047)
- Build the long-lived container/session layer needed for iterative execution.
- Build the structured system prompt.
- Implement the multi-turn worker with its own inner-loop safety: max iterations, worker-local timeout, and budget checks so the worker is safe to run standalone before orchestrator timeout/cancel is added.

## Step 9
Outer timeout/cancel + Vertical Slice E2E (T-042, T-044)
- `T-042` adds the orchestrator-level timeout/cancel envelope around the real worker path.
- `T-044` proves the minimal HTTP submit -> orchestrator -> real worker -> real workspace -> DB -> task_id/status flow.
- DB scope here is execution-path persistence only: task/status, worker run metadata, final result fields, and captured artifacts needed for polling by `task_id`.
- Structured memory wiring (`load_memory` / `persist_memory`) remains part of the later memory integration milestone.

## Step 10
Telegram ingress and generic webhook adapters (T-050 to T-053) on top of the existing HTTP path

## Step 11
Sandbox hardening (T-054) with enforced destructive-action approval gate

## Step 12
Memory integration loop (T-060 to T-064): load_memory -> execute -> persist_learnings

## Step 13
Structured run observability and second worker routing (T-043, T-070+)

## Step 14
Tools, observability, and remaining hardening

## Rule

Do not implement step N+1 until step N has:
- passing tests
- usable logs
- stable local run path

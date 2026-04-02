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
Baseline worker timeout handling + Vertical Slice E2E (T-042, T-044): minimal HTTP submit path + real curl -> orchestrator -> real worker -> real workspace -> DB -> response

## Step 9
Telegram ingress and generic webhook adapters (T-050 to T-053) on top of the existing HTTP path

## Step 10
Sandbox hardening (T-054) with enforced destructive-action approval gate

## Step 11
Memory integration loop (T-060 to T-064): load_memory -> execute -> persist_learnings

## Step 12
Structured run observability and second worker routing (T-043, T-070+)

## Step 13
Tools, observability, and remaining hardening

## Rule

Do not implement step N+1 until step N has:
- passing tests
- usable logs
- stable local run path

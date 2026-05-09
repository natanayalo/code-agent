# Stabilization Tasks (Phase 1: Clarity & Control)

This document details the granular tasks identified during the forensics of trace `b2c3302d...`. These tasks aim to harden the orchestrator, workers, and observability pipeline to ensure reliable e2e execution.

## [T-173] Prompt Simplification & Intent Enforcement
**Use Case**: In Trace `b2c3302d...`, the worker received a cluttered prompt and performed an out-of-scope fix during a "Review" task.
**Investigation**: `workers/prompt.py` currently injects a full tool inventory and file tree even for native agents that auto-discover these. This creates "token junk" and dilutes the goal.
**Instructions**:
- Refactor `build_system_prompt` to omit static tool/file lists when `runtime_mode == NATIVE_AGENT`.
- Inject a strict instruction: "If `delivery_mode` is `review`, do NOT modify files. Report findings only."
- Instruct the agent to read `AGENTS.md` as its first action instead of duplicating policy in the prompt.

## [T-174] Deterministic-First Verification & Timeout Policy
**Use Case**: The verifier timed out (failed) because it ran an LLM check while the environment was broken.
**Investigation**: `orchestrator/graph.py` runs LLM verification regardless of deterministic status. If `pytest` fails, we don't need an LLM to tell us it's broken.
**Instructions**:
- Update `verify_result` node to execute deterministic checks (lint/test/format) first.
- Short-circuit the LLM `IndependentVerifier` if deterministic checks fail.
- Update `orchestrator/verification.py` to return `warning` (Infra Issue) on timeout if internal worker tests previously passed.

## [T-175] Native Infra-Failure Detection & Backpressure
**Use Case**: The agent self-healed a broken environment (`rg` fix), which is high-risk and out of scope.
**Investigation**: `NativeAgentRunner` currently treats shell crashes as generic failures. We need to catch these "infra-level" signals.
**Instructions**:
- Implement `stderr` pattern matching in `workers/native_agent_runner.py` for shell crash markers (`word unexpected`, `Syntax error`).
- Map these to a new `FailureKind.SANDBOX_INFRA`.
- Update the orchestrator to halt and request human clarification when `SANDBOX_INFRA` is detected, rather than retrying blindly.

## [T-176] Phoenix/OpenInference Tracing Standardisation
**Use Case**: Spans are informative but lack structured attributes for easy filtering in Phoenix.
**Investigation**: We are using `OpenInference` but missing key attributes like `input.value` (as JSON) and `output.value` (as JSON) in some nodes.
**Instructions**:
- Update `apps/observability.py` to support recording structured JSON attributes.
- Enrich orchestrator spans with `task_kind`, `route_reason`, and `verification_summary` as top-level attributes.
- Ensure `SPAN_KIND_AGENT` and `SPAN_KIND_TOOL` are consistently applied across all worker types.

## [T-177] Discovery Optimization & Router Resilience
**Use Case**: `generate_task_spec` took 44s and the router failed to fallback during a timeout.
**Investigation**: `OrchestratorBrain` currently lacks a "lightweight" path for simple discovery tasks.
**Instructions**:
- Introduce a "Discovery" profile for workers that skips heavy post-run processing (lint/review).
- Harden the `OrchestratorBrain` fallback logic in `graph.py` to return heuristic results immediately if the model-backed suggestion times out.

## [T-178] E2E Forensic Investigation & Runtime Hardening
**Use Case**: Forensic logs showed `404` polling spam, `ReviewResult` validation failures, and `BrainSuggestion` parsing errors on infra-crashes.
**Investigation**: These failures indicate "silent" logic gaps where the system tries to parse invalid data rather than failing gracefully or reporting the infra-blocker.
**Instructions**:
- **Audit Dashboard Polling**: Fix `404` polling loops in the dashboard state hooks.
- **ReviewResult Resilience**: Log raw candidate text and validation errors in `workers/self_review.py:parse_review_result` for easier debugging.
- **Safe Brain Extraction**: Update `orchestrator/brain.py` to check `WorkerResult.status` before parsing JSON.

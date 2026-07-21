# M24.9.5d — Legacy Runtime Drain Plan

## Decision

Temporal is the normal runtime for new sequential Compose-stack tasks. The
Postgres queue/lease runtime and LangGraph workflow remain an explicit
`CODE_AGENT_EXECUTION_RUNTIME=legacy` fallback until parity and rollback
conditions are completed. This document classifies code; it does not delete a
fallback path.

## Classification

| Area | Current locations | Classification | Drain condition |
| --- | --- | --- | --- |
| Runtime selection | `apps/runtime.py`, submission services | Keep | Retain the explicit selector until an operator-approved rollback alternative exists. |
| Temporal lifecycle | `orchestrator/temporal/` | Primary | Extend for M25; do not duplicate scheduling in the legacy runtime. |
| Queue claims and leases | `repositories/sqlalchemy_task.py`, `orchestrator/execution_runtime_service.py`, `apps/worker/main.py` | Fallback-only deletion candidate | No production-like tasks use legacy after all parity gates pass. |
| WorkerNode dispatch mechanics | `repositories/sqlalchemy_worker.py`, `WorkerNode`, legacy loop | Split | Keep profile/capability/operator policy; claim capacity, leases, stale reclaim, and dispatch quarantine are deletion candidates when Temporal replaces them. |
| LangGraph durable graph/checkpoints | `orchestrator/graph.py`, legacy callers | Fallback-only deletion candidate | Temporal implements every operator interaction family and sequential route. |
| Domain nodes, Worker contract, sandbox, validation, memory | `orchestrator/nodes/`, `workers/`, `sandbox/`, `memory/` | Keep custom | Product logic invoked by both runtimes. |
| Postgres task projections | `db/`, `repositories/` | Keep custom | Dashboard/API read product state, not Temporal history. |

## Required parity before drain

1. [x] Worker-initiated `request_higher_permission` has a persisted Temporal
   interaction, approval/rejection signal, and safe retry/reprovision path.
2. [x] Provider cleanup during cancellation of a running activity is covered.
3. [x] Retryable multi-writer timeline events have stable identities.
4. [x] Supported sequential legacy routes have been compared explicitly with
   Temporal. Parallel DAG execution remains M25 scope, not a drain prerequisite.

## Evidence gate

The M25.3 [Temporal evidence-gate ledger](m25_3_observation_ledger.md)
supersedes the earlier time-based drain period. Before deleting the runtime
selector, record all 14 operational scenarios, coverage for every required task
class, passing unit/integration/pre-commit/dashboard suites, a tagged
last-known-good legacy-capable image, and operator sign-off. Legacy remains an
explicit fallback only until that gate is approved.

## Deletion order

1. Freeze legacy queue/lease and LangGraph lifecycle feature work.
2. Complete the evidence gate and obtain operator sign-off.
3. Disable legacy in production-like Compose while retaining a bounded local
   recovery window.
4. Remove polling, claims, lease heartbeats, and stale reclaim together.
5. Remove LangGraph durable graph/checkpoint execution once no task can select it.
6. Reassess `WorkerNode` fields individually; retain capability/profile policy.

## M25 consequence

M25 uses Temporal for bounded read-only DAG fan-out. M24.9.5 is closed for
pre-M25 purposes; the legacy fallback remains available until the M25.3
evidence gate is approved, not because of an interaction-parity blocker.

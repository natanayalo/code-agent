# M24.9.5c — LangGraph Boundary Audit

## Decision

Temporal is the durable execution owner for the enabled runtime. LangGraph is
legacy orchestration only; it is not a dependency of the Temporal workflow
path. Domain nodes and optional agent reasoning remain reusable code-agent
logic, not Temporal-owned behavior.

Do not introduce a second `BrainPort` or `NodeRunnerPort` abstraction yet:
their narrow equivalents already exist and should be stabilized before any
rename or extraction.

| Target seam | Existing implementation | Decision |
| --- | --- | --- |
| BrainPort | `orchestrator.brain.OrchestratorBrain` protocol | Retain as the brain seam. It supplies advisory TaskSpec/route suggestions and must not own durable sequencing. |
| NodeRunnerPort | `workers.Worker` plus `build_await_result_node` | Retain the Worker contract as the execution seam. Extract a named NodeRunner only if Temporal node execution requires behavior that cannot remain in the Worker contract. |
| Durable lifecycle | `TaskExecutionWorkflow` | Temporal owns sequencing, waits, retries, cancellation delivery, and terminal failure projection. |
| Product projection | Temporal activities plus repositories | Postgres continues to own tasks, interactions, timeline, artifacts, and dashboard reads. |

## Graph ownership inventory

`build_orchestrator_graph()` currently compiles the following legacy LangGraph
sequence in `orchestrator/graph.py`.

| Graph responsibility | Examples | Target owner |
| --- | --- | --- |
| Durable sequencing and branching | `_add_orchestrator_edges`, routing after worker/review outcomes | Temporal workflow for the enabled runtime; legacy LangGraph only during drain. |
| Checkpoints and interruption | LangGraph checkpointer, `interrupt()` in clarification, approval, and permission nodes | Temporal history and signals/updates for the enabled runtime. |
| Task understanding | `ingest_task`, `classify_task`, `plan_task`, repository profile, TaskSpec generation | Code-agent domain nodes; callable from activities. |
| Brain suggestions | `OrchestratorBrain` and TaskSpec/route merge policy | Code-agent brain seam; advisory only. |
| Worker execution | `dispatch_job`, `build_await_result_node`, worker timeout/result validation | Code-agent Worker contract and activity implementation. |
| Workspace and validation | provision/init, verification, review, delivery | Code-agent domain nodes; activities execute them. |
| Memory | load/persist nodes and admission policy | Code-agent domain/memory layer; activities execute them. |

## Temporal-path status

The current Temporal workflow already invokes domain-node equivalents for:

```text
classify/plan → decompose → load memory → provision → run worker
→ verify → deliver → persist memory
```

It does not compile a LangGraph graph or restore a LangGraph checkpoint. This
is the correct direction.

## Interaction parity and LangGraph retention boundary

The legacy graph has three interrupt families:

| Legacy interrupt | LangGraph node | Temporal status | Required direction |
| --- | --- | --- | --- |
| Main task approval | `await_approval` | Implemented as DB interaction plus `handle_approval` signal. | Keep and make the signal payload idempotent. |
| Clarification | `await_clarification` | Implemented and integration-tested: classification exposes the gate, a resolved DB interaction sends `handle_clarification`, and the workflow resumes. | Cancellation while waiting is covered by the Temporal integration suite. |
| Permission escalation | `await_permission`, `await_permission_escalation` | Implemented and deterministically covered: persist a distinct worker escalation interaction, wait for `handle_permission_escalation`, then reject or retry. Grant, rejection, duplicate response, and cancellation cleanup have regression coverage. | Keep the optional Docker workflow exercise as diagnostic coverage; no LangGraph checkpoint is required by the Temporal path. |

Therefore **do not remove LangGraph checkpoint/interrupt support yet**. The
Temporal sequential path has interaction parity for the supported approval,
clarification, and permission-escalation families. LangGraph remains only as
the explicit legacy fallback until the separate drain conditions are met.

## Extraction order

1. Keep Temporal activities and signals on the existing Postgres interaction
   model for all supported interaction families.
2. Keep regression coverage for permission pause/resume, duplicate operator
   decisions, and cancellation while each interaction is pending.
3. Move no domain decision logic into the Temporal workflow; the workflow only
   chooses activities and waits for durable interaction state.
4. Keep LangGraph interrupt/checkpoint behavior legacy-only until the
   M24.9.5d drain conditions are met.

## Non-goals

- Rename `OrchestratorBrain` or `Worker` solely to match a proposed port name.
- Rewrite `graph.py` before interaction parity exists.
- Remove LangGraph dependencies, checkpoints, or the legacy runtime in this
  slice.
- Add DAG fan-out or child workflows.

# M23 Slice 5: Unified Memory Admission

> **Prerequisite**: Slice 4 should be merged first. This slice builds on
> reviewable `memory_proposals`, the realistic retrieval suite, and the
> Postgres full-text retrieval evidence.

## Goal

Unify `WorkerResult.memory_to_persist` and `memory_proposals` into one
memory-admission pipeline.

Worker-produced memories should be treated as **candidate memories**, not as a
direct write request. A dedicated admission service should decide whether each
candidate is rejected, written directly, merged into existing memory, or routed
to human review as a `memory_proposal`.

## Context

Current memory ingestion has two separate write paths:

| Path | Current behavior | Problem |
|---|---|---|
| `WorkerResult.memory_to_persist` | Worker emits memory entries; orchestrator maps them to `PersistMemoryEntry`; `persist_memory` upserts durable personal/project memory directly. | Bypasses the reviewable proposal gate and has no explicit risk/decision record. |
| `memory_proposals` | Operator/API creates pending proposals; human accepts/rejects; accepted proposals upsert durable personal/project memory. | Good review boundary, but not used for worker-produced memory candidates. |

Slice 5 should make `memory_proposals` the human-review queue for risky or
ambiguous candidates, not a parallel memory system. Durable memories remain in
the existing Postgres personal/project tables.

## External Memory-System Research Summary

| System | Useful pattern | Decision for this repo |
|---|---|---|
| LangMem | Semantic/episodic/procedural taxonomy; conscious vs background memory formation; memory manager primitives. | Required Slice 5 adoption spike for extraction/admission assistance behind our interface. Adopt if it reduces custom policy/extraction code without weakening reviewability. |
| Mem0 / OpenMemory | `add` after useful interaction, `search` before model call; ingestion controls, confidence thresholds, sensitive-data filtering, update-vs-delete guidance. | Required Slice 5 adoption spike for extraction/admission assistance behind our interface. Do not move durable storage/retrieval to Mem0 unless the spike proves a clear net simplification. |
| claude-mem | No canonical active project named exactly `claude-mem` was verified during research. Relevant pattern is local-first, auditable, editor/MCP-style memory. | Do not adopt. Keep the local Postgres store and explicit orchestrator admission boundary. |
| Graphiti | Temporal context graph, provenance episodes, fact invalidation, hybrid semantic/keyword/graph retrieval. | Too heavy for M23. Reconsider only if temporal contradictions or relationship-heavy retrieval become proven bottlenecks. |
| Cognee | Relational + vector + graph memory platform with remember/recall/improve/forget lifecycle. | Too broad for this code-agent slice. Reconsider only after simpler Postgres admission/retrieval is exhausted. |

Resolved direction: keep a custom product boundary, not necessarily a fully
custom implementation. Slice 5 should define `MemoryAdmissionService` first,
then run a small LangMem and Mem0/OpenMemory spike behind that interface before
committing to custom extraction/admission logic. The spike should compare code
removed, infrastructure added, reviewability, deterministic tests, local
Postgres compatibility, no-pgvector operation, and safety controls.

Storage remains custom/Postgres in Slice 5 unless the spike proves that moving
storage is a clear net simplification. Retrieval remains FTS-backed unless
measured retrieval misses justify embeddings/vector/graph infrastructure.

Do not add pgvector in this slice. Slice 4 showed Postgres FTS achieved
`1.000` recall on the realistic non-semantic-gap cases for both SQLite fallback
and Postgres FTS. Semantic/vector retrieval should wait for more accepted
memories and real retrieval misses.

## Design Decisions Already Made

| Decision | Choice |
|---|---|
| Durable store | Keep existing Postgres personal/project memory tables. |
| Worker contract | Keep `WorkerResult.memory_to_persist` for compatibility, but treat it as candidate memory. |
| Human review queue | Use `memory_proposals` only for candidates that require human approval. |
| Write owner | Orchestrator owns all memory writes; workers never write memory directly. |
| Retrieval | Keep full-text search. No pgvector/embeddings unless later evals justify it. |
| Admission boundary | Add a `MemoryAdmissionService` between worker results and durable writes/proposals, with library-backed implementations allowed if the spike earns it. |

## Questions To Investigate And Decide In Slice 5

These should be answered during the slice before finalizing implementation:

1. **Admission decision persistence**
   Decide whether to add a new `memory_admission_decisions` table or extend
   `memory_proposals` with admission metadata.

   Recommendation to test first: add a small `memory_admission_decisions` table
   so rejected/direct-write decisions are inspectable without overloading the
   proposal table.

2. **Library adoption vs custom implementation**
   Decide whether LangMem, Mem0/OpenMemory, or custom code should implement the
   first production admission/extraction path behind `MemoryAdmissionService`.

   Required spike:

   - Prototype a LangMem-backed extractor/admission helper against 10 to 20
     realistic task-result candidate cases.
   - Prototype a Mem0/OpenMemory-backed extractor/admission helper against the
     same cases, using local/self-hosted or no-storage mode where possible.
   - Score both against a custom baseline on:
     - amount of code and policy logic removed
     - infrastructure and provider requirements added
     - compatibility with existing Postgres durable memory
     - ability to keep `memory_proposals` as the human-review queue
     - deterministic testability without network calls in CI
     - secret/sensitive-data filtering
     - no-pgvector operation

   Adoption rule: adopt a library for extraction/admission assistance only if it
   is clearly simpler and keeps our safety/review boundaries explicit. Do not
   adopt a library just to get vector or graph retrieval.

3. **Direct-write allowlist**
   Decide which candidate classes may bypass human review.

   Starting recommendation:

   - Allow low-risk, verified, project-scoped facts to write directly.
   - Route personal preferences, communication style, repo conventions, known
     pitfalls, and broad behavioral guidance to human review.
   - Reject secrets, credentials, speculative claims, and unsafe instructions.

4. **Update vs merge semantics**
   Decide how to detect whether a candidate updates an existing memory or
   should merge into its structured `value`.

   Starting recommendation: match on `(category, repo_url, memory_key)`.
   Implement conservative shallow merge only for non-conflicting object keys.
   Conflicts require human review.

5. **Evidence requirements**
   Decide what evidence is required for direct create/update/merge.

   Starting recommendation: direct writes need at least one concrete evidence
   item such as a command/test result, file path, PR URL, migration ID, or
   explicit user instruction. Low-confidence or evidence-free candidates go to
   human review or rejection.

6. **Worker field naming**
   Decide whether to rename `WorkerResult.memory_to_persist` now or keep it
   temporarily.

   Starting recommendation: keep the field for compatibility, update docstrings
   and downstream semantics, and add a future deprecation note for
   `memory_candidates`.

7. **Dashboard visibility**
   Decide whether Slice 5 needs a new admission-decision surface or whether the
   existing Knowledge Base Review tab is enough.

   Starting recommendation: show only human-review proposals in the dashboard
   for this slice; add backend tests and logs for direct/rejected decisions.

## Proposed Contracts

Add a service interface near the orchestrator/memory boundary:

```python
class MemoryAdmissionService:
    def admit_candidates(
        self,
        *,
        task: TaskRequest,
        session: SessionRef | None,
        worker_result: WorkerResult,
        existing_memory: MemoryContext,
    ) -> MemoryAdmissionBatchResult:
        """Classify worker-produced candidate memories and apply admission decisions."""
```

Core DTOs:

```python
MemoryRiskLevel = Literal["low", "medium", "high", "blocked"]
MemoryAdmissionDecision = Literal[
    "reject",
    "create",
    "update",
    "merge",
    "needs_human_review",
]

class MemoryCandidate(OrchestratorModel):
    category: Literal["personal", "project"]
    repo_url: str | None = None
    memory_key: str
    value: dict[str, Any]
    source: str | None = None
    confidence: float = 1.0
    scope: str | None = None
    last_verified_at: datetime | None = None
    requires_verification: bool = True
    evidence: list[str] = Field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None
    producer: Literal["worker", "operator", "system", "import"] = "worker"

class MemoryAdmissionResult(OrchestratorModel):
    candidate: MemoryCandidate
    decision: MemoryAdmissionDecision
    risk_level: MemoryRiskLevel
    reason: str
    durable_memory_id: str | None = None
    proposal_id: str | None = None
```

The existing `WorkerMemoryEntry` can map into `MemoryCandidate`. Add `evidence`
later if the worker contract can safely change in this slice; otherwise derive
evidence from worker commands, test results, files changed, artifacts, and task
metadata.

## Admission Policy V1

### Reject

Reject candidates when any of these apply:

- contains or appears to contain secrets, tokens, credentials, private keys, or
  unredacted sensitive identifiers
- empty `memory_key` or empty/non-object `value`
- project memory cannot be scoped to a repo URL
- candidate is speculative or phrased as an uncertain inference
- candidate attempts to remember unsafe behavior, destructive defaults, auth,
  billing, deployment, or sandbox-policy changes without explicit approval

### Create

Create durable memory directly only when:

- no existing memory exists for the target key/scope
- risk is low
- confidence is high enough, suggested default `>= 0.85`
- evidence exists
- candidate is project-scoped or otherwise mechanically verified

### Update

Update durable memory directly only when:

- the target memory exists
- the candidate clearly supersedes the old value
- there is no conflict that needs human judgment
- evidence exists
- risk is low

### Merge

Merge directly only when:

- target memory exists
- both old and new values are JSON objects
- new keys do not conflict with existing keys
- risk is low

Conflicting merges should become proposals.

### Needs Human Review

Create `memory_proposals` for:

- personal preferences and communication preferences
- repo conventions
- workflow notes
- known pitfalls
- approval defaults
- medium/high-risk candidates
- conflicts with existing memory
- candidates with useful content but insufficient evidence for direct write

## Implementation Steps

### 1. Rename the semantics, not necessarily the field

**Files**: `workers/base.py`, `orchestrator/state.py`, tests

- Update `WorkerMemoryEntry` docstring from "for orchestrator persistence" to
  "candidate memory for orchestrator admission".
- Update `WorkerResult.memory_to_persist` docstring or field description to
  say it is not a durable-write guarantee.
- Keep serialized field name for compatibility.

### 2. Add admission DTOs and service interface

**New file**: `memory/admission.py` or `orchestrator/memory_admission.py`

Implement the interface and a deterministic custom baseline implementation:

- candidate normalization from `WorkerMemoryEntry`
- risk classification
- policy decision
- durable create/update/merge through existing repositories
- proposal creation through `MemoryProposalRepository`
- structured result objects for timeline/logging/tests

### 3. Run library adoption spike before expanding custom logic

**Files**: `docs/m23-slice-5-memory-admission.md`,
`evaluation/` or `tests/unit/test_memory_admission_spike.py`

Before building complex custom extraction/admission code, compare LangMem and
Mem0/OpenMemory behind the `MemoryAdmissionService` boundary.

The spike should use a small checked-in fixture of candidate-producing task
summaries and expected admission outcomes, including:

- low-risk verified project fact
- personal communication preference
- repo convention
- known pitfall
- conflicting update
- secret-like candidate
- whitespace/invalid scope
- evidence-free but plausible candidate

Record a short decision note in this doc or a follow-up report:

- adopt LangMem for extraction/admission assistance
- adopt Mem0/OpenMemory for extraction/admission assistance
- keep custom for Slice 5 and revisit after more corpus/eval evidence

Do not wire any network-dependent library behavior into CI tests. If a library
is adopted, wrap it behind an adapter and keep deterministic unit tests around
the admission policy.

### 4. Add admission decision persistence

**Files**: `db/models.py`, `db/enums.py`, `db/migrations/versions/<new_revision>.py`

Investigate first whether a new table is worthwhile. If yes, add:

- `memory_admission_decisions`
- candidate payload snapshot
- decision
- risk level
- reason
- task_id/session_id
- durable_memory_id or proposal_id
- created_at

If the slice decides not to add a table, at minimum emit timeline payloads and
logs for all decisions.

### 5. Replace direct persist mapping

**File**: `orchestrator/graph.py`

Replace `_map_worker_memory_to_persist(...)` direct mapping with admission:

- `summarize_result` should collect worker candidate memories.
- `persist_memory` should call `MemoryAdmissionService`.
- The node should no longer blindly upsert every worker entry.
- Timeline payload should include counts by decision and risk level.

The plain fallback graph can keep no-op behavior when no session factory is
available, but the DB-backed graph should use admission.

### 6. Keep proposal accept/reject as the human gate

**Files**: `repositories/sqlalchemy_memory_proposal.py`,
`orchestrator/execution_snapshot_service.py`, API/dashboard tests

- Accepted proposals still upsert durable memory.
- Rejected proposals remain terminal.
- Admission-created proposals should include task/session/source/evidence.
- Dashboard Review tab should continue to show pending proposals only.

### 7. Tests

Add targeted tests for:

- low-risk verified project candidate writes directly
- personal preference becomes pending proposal
- repo convention becomes pending proposal
- conflicting update becomes pending proposal
- secret-like candidate is rejected
- project candidate without repo URL is rejected or skipped with a decision
- merge succeeds for non-conflicting object values
- rejected proposal cannot later be accepted
- admission timeline payload records decision counts
- existing Slice 4 proposal API tests continue to pass

## Verification Plan

Run narrow tests first:

```bash
.venv/bin/pytest tests/unit/test_memory_admission.py
.venv/bin/pytest tests/unit/test_persist_memory_node.py
.venv/bin/pytest tests/integration/test_repositories_memory_proposal.py
.venv/bin/pytest tests/integration/test_knowledge_base_endpoints.py
```

Then broader relevant checks:

```bash
.venv/bin/pytest tests/unit
.venv/bin/pytest tests/integration
cd dashboard && npm run test:coverage
.venv/bin/pre-commit run --all-files
```

If dashboard behavior changes, add/adjust Knowledge Base component tests and
run:

```bash
cd dashboard && npm run test:run -- KnowledgeBasePage api
```

## Non-Goals

- Do not add pgvector, embeddings, or semantic retrieval.
- Do not move durable memory storage out of the existing Postgres personal/project tables unless the Slice 5 spike explicitly proves that a library-backed store is a clear net simplification.
- Do not add Graphiti or Cognee as dependencies in Slice 5.
- Do not add LangMem or Mem0/OpenMemory as production dependencies before the required adoption spike and decision note.
- Do not make workers write directly to the database.
- Do not auto-store every task summary or transcript.
- Do not expand memory categories beyond personal/project/session state.
- Do not reuse the generic `proposals` table for memory acceptance.

## Expected End State

After Slice 5:

- all worker-produced memories pass through one admission service
- direct durable writes are limited to low-risk, evidenced candidates
- human-review candidates become `memory_proposals`
- rejections are inspectable through logs/timeline and, if chosen, admission
  decision persistence
- durable personal/project memories remain in Postgres
- retrieval remains FTS-backed until realistic misses justify semantic/vector
  work

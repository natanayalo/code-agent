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
| [claude-mem](https://github.com/thedotmack/claude-mem) | Persistent context across coding-agent sessions using lifecycle hooks, a local worker service, SQLite/FTS5, MCP search tools, progressive disclosure, and optional Chroma hybrid search. | Corrected after follow-up research: it is real and relevant as an agent-memory/retrieval architecture reference. Do not adopt for Slice 5 admission because it replaces too much of this repo's capture/storage/retrieval path instead of providing a small extraction/admission helper. |
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

## Slice 5 Spike Decision

The adoption spike was run locally before finalizing production code. The
experimental libraries were installed only in the local virtualenv and were not
added to `pyproject.toml`. Prototype scripts and adapter placeholders were
removed from the PR after the experiment so the repository only carries the
production boundary and this decision record.

Local experiment summary:

- `CustomMemoryAdmissionService` matched all eight checked-in admission fixture
  outcomes deterministically: direct write for a verified project fact, review
  routing for personal/convention/pitfall/conflict/evidence-free candidates,
  and rejection for secret-like or invalid-scope candidates.
- LangMem `create_manage_memory_tool` and `create_search_memory_tool` can run
  locally over a LangGraph `InMemoryStore`, but those store tools do not
  simplify this repo's Postgres durability or review queue.
- LangMem extraction with `gemma4:12b-mlx` produced useful structured
  candidates in prior local runs and avoided redacted-token, speculative, and
  temporary branch/commit snippets. The tradeoff was high latency: roughly
  minutes for bundled traces, with one fresh bundled run interrupted after
  several minutes without a result.
- LangMem extraction with `qwen2.5-coder:7b` returned zero candidates for the
  bundled trace in about 41s. Direct Qwen structured output worked, so the
  issue appears to be the LangMem/trustcall tool-call path rather than the
  model's basic ability to emit JSON.
- Mem0 can run without OpenAI when configured with local Ollama
  `qwen2.5-coder:7b`, `embeddinggemma:latest` embeddings, and file-backed
  Qdrant. Simple isolated `infer=True` probes extracted a preference in about
  25s and a repo fact in about 23s.
- Mem0 failed the bundled admission-quality test: it extracted all seven
  snippets, including redacted-token guidance, a speculative Vitest claim, and
  a temporary branch/commit detail. It also persisted plain text in Mem0/Qdrant,
  not this repo's structured Postgres memory tables.
- A direct custom structured-output probe with `qwen2.5-coder:7b` performed
  best among the model-assisted experiments: with an explicit schema and prompt,
  it produced four correct durable candidates and three correct rejections in
  about 50s.
- Follow-up research corrected the earlier `claude-mem` note. `claude-mem`
  should have been included in the landscape review because it demonstrates
  useful patterns for hooks-based observation capture, progressive-disclosure
  retrieval, and MCP/HTTP search. It was not added to the runnable Slice 5
  experiment because it is a full memory capture, compression, storage, and
  retrieval stack rather than a focused candidate-extraction/admission library.

### claude-mem Replacement Experiment

After the initial Slice 5 cleanup, `claude-mem` was evaluated locally as a
possible replacement for the current memory subsystem, not just as an admission
helper.

Experiment setup:

- Cloned `thedotmack/claude-mem` v13.10.1 into `/private/tmp`.
- Ran the bundled worker with a redirected `HOME`, `CLAUDE_CONFIG_DIR`, and
  `CLAUDE_MEM_DATA_DIR`, so no real Claude/Codex configuration was modified.
- Disabled Chroma, Telegram, and semantic injection for the first pass to test
  SQLite-only behavior.
- Sent synthetic Codex-shaped session and tool events through the local HTTP
  API (`/api/sessions/init`, `/api/sessions/observations`,
  `/api/sessions/summarize`) instead of installing hooks.

Findings:

- The worker starts cleanly in isolated SQLite-only mode and exposes useful
  health, search, recent-context, timeline, and context-injection endpoints.
- Without a configured generator, it persists the user prompt but does not
  create searchable observations from queued tool events. This is a major
  replacement difference from the current deterministic memory writes.
- With `CLAUDE_MEM_PROVIDER=openrouter`,
  `CLAUDE_MEM_OPENROUTER_BASE_URL=http://127.0.0.1:11434/v1`, and local
  `qwen2.5-coder:7b`, queued events were compressed into observations. The
  prompt plus three tool observations and one summary took roughly 2.5 minutes
  end-to-end on the local model.
- Search worked well after compression. A `pytest` query found both the test
  command observation and the AGENTS.md convention observation. The
  context-injection endpoint produced a concise session context block.
- `<private>...</private>` content was stripped before persistence; a search for
  the synthetic `api_token` returned no results.
- The model-assisted compression stored a speculative Vitest note as a
  `bugfix` observation and propagated it into the session summary's learned and
  next-steps fields. That is useful as session history, but unsafe as accepted
  project memory without a review/admission layer.

Replacement assessment:

`claude-mem` looks promising for replacing or augmenting the **episodic session
memory and progressive retrieval** layer: hooks-based capture, local worker,
viewer, MCP/HTTP search, SQLite FTS, context injection, and optional semantic
search are all stronger and more complete than this repo's current ad hoc
session-memory surface.

It is not a drop-in replacement for the **curated durable memory** layer in
Slice 5. Replacing current memory wholesale would regress:

- Postgres durability for personal/project memory.
- Inspectable `memory_proposals` review before accepting broad preferences,
  conventions, pitfalls, or speculative claims.
- Deterministic tests for direct-write/reject/review decisions.
- Synchronous admission guarantees for worker-emitted candidates.
- Safety controls beyond explicit private tags and model prompt behavior.

Follow-up recommendation: implement
[`M23 Slice 6: Episodic Observation Layer`](m23-slice-6-episodic-observation-layer.md).
Copy the useful library patterns into this repo's local architecture: raw
task/session observation capture, compact search, timeline/full-observation
fetches, recent-session context blocks, private-tag stripping, and an
observation-to-`MemoryCandidate` bridge. Keep `MemoryAdmissionService`, Postgres
personal/project tables, and `memory_proposals` as the curated durable memory
path. Do not replace the curated admission layer with observations without a
deterministic admission gate.

| Criterion | Custom baseline | LangMem experiment | Mem0/OpenMemory experiment |
|---|---|---|---|
| Code/policy removed | None; policy remains explicit and small. | Candidate extraction could remove future custom extractor code, but not admission policy. | Could help turn raw task text into short memory wording, but does not remove admission policy. |
| Infrastructure/provider requirements | None. | Package install pulls LangChain provider integrations; extraction needs a model runtime with compatible tool calling, such as Gemma via local Ollama in this spike. | Package install pulls Qdrant/PostHog/provider stack; default path needs OpenAI embedding access. |
| Local Postgres durability | Preserved. | Tooling can run over LangGraph stores, but no direct simplification for existing Postgres tables was proven. | Local Ollama/Qdrant storage works, but it is separate from this repo's Postgres durable tables. |
| Reviewable proposal queue | Preserved through `memory_proposals`. | Fits best as an extractor before admission; all outputs still need the custom admission gate. | Local inference can extract plain-text memories, but it bypasses `memory_proposals`; outputs still need mapping into candidates and admission decisions. |
| Deterministic CI | Fully deterministic. | Extraction is not deterministic enough for CI with a live local model; adapter tests still need a fake chat model. | Local add/search/infer can run with Ollama embeddings, but depends on local model/vector-store setup and emits optional telemetry/extras warnings. |
| Secret/safety controls | Explicit local checks before write/proposal. | Could run before/after LangMem extraction, but not proven as code reduction. | Would still need explicit pre-admission filtering and proposal policy. |
| No-pgvector operation | Preserved. | Preserved for local LangGraph store tooling. | Not aligned with no-vector baseline; default stack is vector-store centric. |

Decision: keep the custom deterministic baseline for production admission in
Slice 5. Do not adopt LangMem or Mem0/OpenMemory as production dependencies in
this PR. Neither clearly simplifies extraction/admission while preserving
reviewability, local Postgres durability, deterministic tests, and safety
controls. The best follow-up candidate is a small optional local
structured-output extractor that emits `MemoryCandidate` DTOs and then passes
through the deterministic admission service; that should be a separate spike
with fake-model tests and no durable storage changes.

## Design Decisions Already Made

| Decision | Choice |
|---|---|
| Durable store | Keep existing Postgres personal/project memory tables. |
| Worker contract | Keep `WorkerResult.memory_to_persist` for compatibility, but treat it as candidate memory. |
| Human review queue | Use `memory_proposals` only for candidates that require human approval. |
| Write owner | Orchestrator owns all memory writes; workers never write memory directly. |
| Retrieval | Keep full-text search. No pgvector/embeddings unless later evals justify it. |
| Admission boundary | Add a `MemoryAdmissionService` between worker results and durable writes/proposals. |

## Decisions Made In Slice 5

1. **Admission decision persistence**
   Add a small `memory_admission_decisions` table so rejected/direct-write
   decisions are inspectable without overloading the proposal table.

2. **Library adoption vs custom implementation**
   Use custom deterministic admission for the first production path. The local
   LangMem and Mem0/OpenMemory experiments did not prove a clear simplification
   that preserves reviewability, Postgres durability, deterministic tests, and
   safety controls.

3. **Direct-write allowlist**
   Allow low-risk, verified, project-scoped facts to write directly. Route
   personal preferences, communication style, repo conventions, known pitfalls,
   and broad behavioral guidance to human review. Reject secrets, credentials,
   speculative claims, and unsafe instructions.

4. **Update vs merge semantics**
   Match project memory by `(repo_url, memory_key)`. Apply conservative shallow
   merge only for non-conflicting object keys. Conflicts require human review.

5. **Evidence requirements**
   Direct writes need at least one concrete evidence item such as a command/test
   result, file path, PR URL, migration ID, or explicit user instruction.
   Low-confidence or evidence-free candidates go to human review or rejection.

6. **Worker field naming**
   Keep `WorkerResult.memory_to_persist` for compatibility, but document and
   handle it as candidate memory rather than a durable-write guarantee.

7. **Dashboard visibility**
   Show only human-review proposals in the dashboard for this slice. Direct and
   rejected decisions are inspectable through backend persistence and tests.

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

### 3. Record the library adoption spike decision

**Files**: `docs/m23-slice-5-memory-admission.md`,
`tests/fixtures/memory_admission_spike_cases.json`,
`tests/unit/test_memory_admission.py`

The LangMem and Mem0/OpenMemory experiments were run locally before production
wiring. The repository keeps the deterministic fixture contract and the
decision note, but does not keep manual experiment scripts or optional-library
adapter placeholders in the production diff.

The checked-in fixture covers:

- low-risk verified project fact
- personal communication preference
- repo convention
- known pitfall
- conflicting update
- secret-like candidate
- whitespace/invalid scope
- evidence-free but plausible candidate

Decision: keep custom deterministic admission for Slice 5 and revisit
model-assisted extraction only after more corpus/eval evidence. Do not wire
network-dependent library behavior into CI tests.

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

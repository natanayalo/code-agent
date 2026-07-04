# M23 Slice 6: Episodic Observation Layer

## Goal

Add an episodic observation layer that captures raw task/session observations,
supports compact retrieval, and can propose durable memory candidates without
turning every observation into accepted memory.

Slice 6 should copy the best ideas from LangMem, Mem0/OpenMemory, and
claude-mem as local product patterns: capture episodes, compress them for
context, retrieve recent and relevant observations, strip private content, and
bridge useful observations into `MemoryCandidate` objects for
`MemoryAdmissionService`.

## Why This Follows Slice 5

Slice 5 deliberately keeps durable personal/project memory curated, reviewable,
and Postgres-backed. The local library experiments showed that memory systems
are strongest at observation capture, progressive disclosure, and context
retrieval, but weakest for this repo when they bypass deterministic admission or
store speculative claims as durable memory.

Slice 6 should therefore add an observation layer beside durable memory:

- observations are useful task/session history
- durable memory remains accepted personal/project memory
- risky or broad claims still flow through `MemoryAdmissionService`

## Library Ideas To Reuse

| Source | Idea to copy | Local adaptation |
|---|---|---|
| claude-mem | Hooks-style session/task observation capture, recent context block, timeline fetch, search endpoint, private-tag stripping, progressive disclosure. | Capture orchestrator and worker events into local Postgres observations, expose recent/search/timeline fetches, and strip private-tagged content before storage. |
| LangMem | Episodic vs semantic/procedural separation; conscious extraction after interaction instead of direct storage. | Treat observations as episodic records and use a bridge to propose `MemoryCandidate` objects only after filtering/extraction. |
| Mem0/OpenMemory | `add` useful interaction, `search` before model call, confidence/sensitivity filtering, update-vs-ignore guidance. | Store compact searchable observations after task events, retrieve before dispatch, and keep sensitivity filtering before admission. |

The goal is not to add these libraries as production dependencies. The goal is
to absorb the patterns that fit this repo's safety and durability model.

## Scope

1. **Store raw task/session observations**
   - Add an inspectable observation table for task/session events.
   - Include source event type, task/session IDs, repo URL, worker type when
     available, timestamp, summary text, structured metadata, and privacy flags.
   - Keep the raw observation separate from durable personal/project memory.

2. **Strip private-tagged content**
   - Remove or redact `<private>...</private>` content before persistence.
   - Record that stripping occurred without storing the stripped content.
   - Apply this before search indexing, candidate extraction, or context blocks.

3. **Add compact search**
   - Start with Postgres full-text search and SQLite-compatible fallback.
   - Do not add pgvector, Chroma, Qdrant, embeddings, or network-dependent
     search in this slice.
   - Search should return observation snippets with source/timestamp metadata.

4. **Add timeline/full-observation fetch**
   - Allow fetching a task/session observation timeline.
   - Allow fetching a full observation by ID for inspection/debugging.
   - Keep this operator-facing and inspectable.

5. **Add recent-session context block**
   - Build a compact recent-context block from recent observations for the same
     session/repo.
   - Keep it clearly labeled as observations, not durable memory.
   - Preserve existing durable memory context loading separately.

6. **Bridge observations into memory admission**
   - Add a bridge that can turn selected observations into `MemoryCandidate`
     objects.
   - Run every bridged candidate through `MemoryAdmissionService`.
   - Durable writes and proposals remain owned by the admission service.

## Non-Goals

- Do not persist observations as durable memory.
- Do not replace personal/project memory tables.
- Do not bypass `memory_proposals`.
- Do not add LangMem, Mem0/OpenMemory, claude-mem, Chroma, Qdrant, pgvector, or
  embeddings as production dependencies.
- Do not make model-generated observations trusted facts.
- Do not require network calls or local LLM availability in CI.

## Proposed Shape

Suggested table:

- `memory_observations`
- `id`
- `task_id`
- `session_id`
- `repo_url`
- `worker_type`
- `source`
- `event_type`
- `observed_at`
- `summary`
- `content`
- `metadata`
- `privacy_stripped`
- `requires_admission`

Suggested service boundaries:

- `ObservationRepository`: create, get, list timeline, search, recent.
- `ObservationCaptureService`: normalize task/session/worker events into
  observations.
- `ObservationContextService`: build compact recent-session context blocks.
- `ObservationMemoryBridge`: extract or select candidate memories from
  observations and submit them to `MemoryAdmissionService`.

## Tests

- Unit tests for private-tag stripping, observation normalization, recent-context
  formatting, and observation-to-candidate mapping.
- Repository tests for create/get/timeline/search against SQLite fallback and
  Postgres migration compatibility.
- Orchestrator tests proving observations are captured without durable memory
  writes.
- Admission bridge tests proving candidates still route through
  `MemoryAdmissionService`.
- Regression tests for speculative observations: searchable as observations,
  not accepted as durable memory unless admission approves them.

## Definition Of Done

- A task can store inspectable observations without creating durable memory.
- Recent observations can be loaded into a clearly labeled context block.
- Observation search and timeline fetch work locally without embeddings.
- Private-tagged content is stripped before persistence.
- Observation-derived candidates pass through the existing admission service.
- Slice 5 curated durable memory behavior remains unchanged.

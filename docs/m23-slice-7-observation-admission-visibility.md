# M23 Slice 7: Observation And Admission Visibility

> **Prerequisite**: Slice 6 should be merged first. This slice builds on the
> existing `memory_observations`, `memory_admission_decisions`, and
> `source_observation_id` lineage fields that are already persisted and tested.

## Goal

Expose observation and admission lineage through API and dashboard surfaces so
operators can answer:

- why was this memory created?
- why was this candidate rejected?
- which observation caused this proposal or direct write?

This slice is about **inspectability**, not new admission policy. It should
make the Slice 5 and Slice 6 pipeline debuggable before broadening automatic
candidate extraction or changing read-side behavior.

## Why This Is The Right Next Slice

Slice 6 already added most of the backend foundation:

- `memory_observations` persistence
- `memory_admission_decisions` persistence
- `source_observation_id` on proposals and decisions
- recent-observation loading into worker context
- a bridge from pending observations into `MemoryAdmissionService`

What is missing is operator visibility. Today, the dashboard exposes accepted
memory and reviewable proposals, but not the observation/admission chain behind
them. That makes it hard to debug whether the system is forming the right
candidates before we expand extraction breadth.

This slice is intentionally narrower and safer than the other candidate ideas:

- it reuses existing persisted data instead of inventing new heuristics
- it improves debugging before changing write or read behavior
- it gives us evidence for the next extraction and retrieval-policy slices

## Validation Of The Proposed Ideas

### 1. Observation/admission visibility

Validated as the best immediate next slice.

Reasoning:

- The database and repository layer already support observations, admission
  decisions, and lineage.
- The API/dashboard surface does not yet expose them.
- This yields fast operator value with low policy risk.
- It improves debugging for every later memory slice.

### 2. Candidate extraction from real traces

Validated, but not as the immediate next slice.

Reasoning:

- Deterministic extraction is the right starting direction.
- The current bridge only accepts pre-structured
  `metadata_payload["memory_candidate"]`, so there is room to learn more
  automatically.
- However, expanding extraction before visibility would make it harder to debug
  false positives and false negatives.

Recommended follow-up after this slice:

- successful test command -> verification-command candidate
- failed command followed by successful replacement -> pitfall candidate
- explicit "remember this" interaction/user text -> memory candidate
- AGENTS.md / repo-doc convention evidence -> repo-convention candidate

### 3. Read-side memory gate

Validated as important, but belongs in a later dedicated slice.

Reasoning:

- This changes worker-visible behavior, not just inspectability.
- The current load path retrieves durable memory plus recent observations but
  does not yet score staleness/conflict/risk or advisory strength.
- A read gate needs retrieval-policy rules, evaluation fixtures, and
  regression coverage because bad gating could hide useful context or surface
  conflicting guidance.

This should follow visibility and deterministic extraction so the gate has
better evidence inputs and a debuggable corpus.

### 4. Repo profile from accepted memory

Validated as a good downstream productization step, not the next slice.

Reasoning:

- It is valuable only after accepted memories are rich and trustworthy enough.
- It depends on both better candidate extraction and a read-side gate so the
  profile is compact, relevant, and safe to inject.
- It is a consumer of the earlier slices, not a foundation slice itself.

## Scope

1. **Observation list and detail API**
   - Add operator-authenticated endpoints to list/search observations by repo,
     session, task, source, event type, and admission status.
   - Add a detail endpoint for one observation, including redaction status and
     structured metadata.

2. **Admission decision list API**
   - Add endpoints to list direct-write, merge, update, review, and reject
     outcomes from `memory_admission_decisions`.
   - Include links to `proposal_id`, `durable_memory_id`, and
     `source_observation_id` when present.

3. **Lineage join surface**
   - Expose compact lineage data so the UI can traverse:
     observation -> admission decision -> proposal or durable memory
   - Expose the reverse direction where useful:
     proposal -> source observation

4. **Dashboard visibility**
   - Add an observation/admission tab or subview in the Knowledge Base area.
   - Support searching/filtering observations and decisions.
   - Show lineage badges/links on proposal review cards and reviewed proposals.

5. **Task/session inspection hooks**
   - Surface task-scoped and session-scoped observations from task detail where
     useful, without duplicating the full timeline UI.
   - Keep this operator-facing and inspectable.

## Non-Goals

- Do not broaden automatic candidate extraction in this slice.
- Do not change `MemoryAdmissionService` decision policy.
- Do not change what is injected into workers during `load_memory`.
- Do not introduce embeddings, semantic retrieval, or model-based extraction.
- Do not auto-accept observations as durable memory.
- Do not redesign the entire Knowledge Base UX beyond what is needed for
  visibility and lineage inspection.

## Proposed API Shape

Suggested additions under `/knowledge-base`:

- `GET /knowledge-base/observations`
- `GET /knowledge-base/observations/{observation_id}`
- `GET /knowledge-base/admission-decisions`

Suggested query filters:

- `repo_url`
- `task_id`
- `session_id`
- `source`
- `event_type`
- `admission_status`
- `decision`
- `limit`
- `offset`
- `q`

Suggested snapshot fields:

### Observation snapshot

- `observation_id`
- `task_id`
- `session_id`
- `repo_url`
- `worker_type`
- `source`
- `event_type`
- `observed_at`
- `summary`
- `content`
- `metadata_payload`
- `privacy_stripped`
- `admission_status`
- `admission_processed_at`
- `admission_error`

### Admission decision snapshot

- `decision_id`
- `category`
- `memory_key`
- `candidate_payload`
- `decision`
- `risk_level`
- `reason`
- `task_id`
- `session_id`
- `durable_memory_id`
- `proposal_id`
- `source_observation_id`
- `created_at`

## Dashboard UX Notes

Keep the first UX pass simple and inspectable:

- Reuse the existing Knowledge Base page instead of creating a separate memory
  admin area.
- Add a dedicated tab such as `Trace` or `Observations`.
- Default to recent items with filters instead of an always-expanded full graph.
- Show lineage as plain linked pills or cards, not a heavy visual graph.
- Preserve the current browse/review/add flows for durable memory and proposals.

## Tests

- API integration tests for observation listing, detail fetch, and admission
  decision listing.
- Serialization tests for new snapshot DTOs.
- Dashboard service tests for new endpoints and malformed payload fallback.
- Dashboard component tests for filters, empty states, lineage rendering, and
  observation/admission navigation.
- Regression coverage proving private-tag redaction remains enforced in surfaced
  observation payloads.

## Definition Of Done

- Operators can inspect observations and admission decisions through API and
  dashboard surfaces.
- Proposal and decision views can point back to the source observation when one
  exists.
- Direct-write and reject outcomes are visible without digging through raw DB
  rows or logs.
- The UI clearly distinguishes:
  - raw observations
  - admission decisions
  - reviewable proposals
  - accepted durable memory
- Slice 5 and Slice 6 behavior stays unchanged apart from new visibility.

## Recommended Follow-Up Sequence

After this slice:

1. deterministic trace-to-candidate extraction
2. read-side memory gate for relevance/staleness/conflict/risk
3. compact repo profile synthesized from accepted project memory

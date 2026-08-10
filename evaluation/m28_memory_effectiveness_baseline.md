# M28.1 Paired Memory Effectiveness Baseline

## What this measures

This deterministic baseline runs the database-backed `load_memory` path twice
for each scenario: a cold task with no memory or compact-session state, then
the same repository/task context with pre-admitted skeptical-memory fixtures.

The four checked-in cases verify that the current full-text and read-side gate
path can:

- make one fresh, relevant project memory available to the worker context;
- keep an unrelated fixture out of the worker context;
- suppress stale high-risk deployment guidance and label it for re-verification;
- resolve a conflicting personal/project value in favor of the project value.

Each assisted case also asserts exact recovery of the active goal, decisions,
risks, and touched files from compact session state.

## Deterministic result

Run the SQLite baseline with:

```bash
.venv/bin/python scripts/e2e/run_memory_effectiveness_eval.py
```

The expected result is four passing cases and a JSON artifact at
`artifacts/evaluations/m28-memory-effectiveness-report.json`. The report keeps
the source, confidence, scope, verification state, retrieval, gate decision,
worker-context availability, and reason codes for every fixture.

The initial baseline verification passed all four cases against both the SQLite
fallback and a freshly migrated disposable PostgreSQL database. The report
labels these backends separately as `sqlite_substring_fallback` and
`postgres_full_text`; the existing timeline's logical retrieval label is kept
separately for diagnostics.

`available_to_worker` means the value was serialized into the worker-visible
memory context. It does not mean a worker used the value to complete work.
Likewise, `pre_admitted_fixture` denotes a test seed; this baseline does not
evaluate write-side memory admission.

## Conclusion and next evidence

The baseline confirms safe context delivery and rejection behavior on four
synthetic paired cases. It does not prove fewer repeated questions,
interventions, or faster validated results, so it does not complete M28 and
does not justify semantic or vector retrieval infrastructure. A later M28
slice must collect paired real-worker task evidence before making either claim.

# M23 Slice 4 Memory Retrieval Conclusion

## Inputs

- Synthetic smoke suite: `evaluation/memory_retrieval_suite.json`
- Realistic curated suite: `evaluation/memory_retrieval_realistic_suite.json`
- Retrieval mode under test: full-text search through the existing `load_memory` path
- Explicitly out of scope: pgvector, embeddings, semantic ranking infrastructure

## Realistic Curated Corpus

The realistic suite seeds 12 review-ready memories covering:

- communication preferences
- routing preferences
- Python/dashboard verification commands
- dashboard memory-review workflow
- DB migration policy
- known repo pitfalls
- git and PR workflow
- approval policy
- critical-path testing policy
- worker/provider ownership boundaries
- status/roadmap conventions
- definition of done

These entries are intentionally small and reviewable. In production, the same content should be created as `memory_proposals`, accepted by the operator, and only then persisted as durable personal/project memory.

## Deterministic Result

Commands:

```bash
.venv/bin/python scripts/e2e/run_memory_retrieval_eval.py --suite evaluation/memory_retrieval_realistic_suite.json --output /tmp/memory-realistic-sqlite-report.json --fail-under-recall 1.0
.venv/bin/python scripts/e2e/run_memory_retrieval_eval.py --suite evaluation/memory_retrieval_realistic_suite.json --output /tmp/memory-realistic-postgres-report.json --postgres-url-env CODE_AGENT_TEST_POSTGRES_URL --fail-under-recall 1.0
```

Use `--database-url` / `--postgres-url-env` only with a disposable test database; the runner applies migrations and seeds evaluation memories.

Observed backend comparison:

| Backend | Cases | Non-semantic-gap recall | Regression misses | Known semantic-gap misses |
| --- | ---: | ---: | ---: | ---: |
| SQLite fallback | 9 | 1.000 | 0 | 3 |
| Postgres FTS | 9 | 1.000 | 0 | 3 |

Known semantic-gap misses in both runs:

- `known-gap-definition-of-done:project:definition_of_done`
- `known-gap-migration-validation-synonym:project:db_migration_policy`
- `known-gap-worker-boundary-synonym:project:worker_boundaries`

## Conclusion

Current evidence does not justify starting semantic/vector retrieval yet.

Full-text search retrieves deliberately worded memories reliably, and the realistic corpus is still too small to prove that embedding infrastructure would improve real task outcomes enough to justify its operational cost. The three marked misses are useful evidence of semantic gaps, but they are better handled in the next slice by improving memory wording and collecting more accepted proposals before comparing semantic retrieval.

Recommended next step: keep improving the curated corpus and memory phrasing, then run a later FTS-vs-semantic comparison once accepted memories and real prompts are numerous enough to measure quality beyond isolated synonym misses.

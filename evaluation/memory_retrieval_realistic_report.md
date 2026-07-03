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

Command:

```bash
.venv/bin/python scripts/e2e/run_memory_retrieval_eval.py --suite evaluation/memory_retrieval_realistic_suite.json --output /tmp/memory-realistic-report.json --fail-under-recall 1.0
```

Observed result:

- cases: 9
- non-semantic-gap recall: 1.000
- regression misses: 0
- known semantic-gap misses: 3

## Conclusion

Current evidence does not justify starting semantic/vector retrieval yet.

Full-text search retrieves deliberately worded memories reliably, and the realistic corpus is still too small to prove that embedding infrastructure would improve real task outcomes enough to justify its operational cost. The three marked misses are useful evidence of semantic gaps, but they are better handled in the next slice by improving memory wording and collecting more accepted proposals before comparing semantic retrieval.

Recommended next step: keep improving the curated corpus and memory phrasing, then run a later FTS-vs-semantic comparison once accepted memories and real prompts are numerous enough to measure quality beyond isolated synonym misses.

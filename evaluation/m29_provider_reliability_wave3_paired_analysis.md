# M29 Wave 3 Paired Analysis

- **Suite**: `m29-live-provider-evidence-wave3`
- **Build SHA**: `a32719d114fab93c8e63e81153229ac900748a5c`
- **Manifest SHA-256**: `3c4b31645c16d2ec72e9f45f318731165cb917e8b0229838cfd2f45df4691e70`
- **Observation Reference (`as_of`)**: `2026-09-19T21:32:22.562107+00:00`

This supplement resamples complete topic pairs; it is descriptive and does not change routing.

| Task Class | Pairs | Both Completed | Codex Only | Antigravity Only | Neither | Identity-Complete Pairs | Successful Delta N | Median AG-Codex (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `feature` | 10 | 5 | 0 | 0 | 5 | 10 | 5 | -150.79 |
| `investigation` | 10 | 3 | 0 | 2 | 5 | 5 | 3 | -552.85 |

## Paired Bootstrap

### `feature`
- 10,000 iterations, seed 29; 10 identity-complete pairs.
- Antigravity higher acceptance: `0.0000`; Codex higher: `0.0000`; tied: `1.0000`.
- Antigravity faster successful latency: `1.0`.

### `investigation`
- 10,000 iterations, seed 29; 5 identity-complete pairs.
- Antigravity higher acceptance: `0.6761`; Codex higher: `0.0000`; tied: `0.3239`.
- Antigravity faster successful latency: `1.0`.

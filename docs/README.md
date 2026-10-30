# Documentation map

## Planning and contribution

| Document | Purpose |
| --- | --- |
| [Status and current backlog](status.md) | Current phase, ordered implementation queue, maintenance/approval lanes, and evidence gates. Start here to select work. |
| [Roadmap](roadmap.md) | Milestone goals, boundaries, exit criteria, and conditional future directions. |
| [Contributor guide](../AGENTS.md) | Canonical repository policy, ownership, safety, testing, and delivery requirements. |
| [Changelog](../CHANGELOG.md) | Generated history of merged pull requests. |

GitHub issues contain detailed slice acceptance checklists; status owns priority
order. Update both when scope or state changes. Completed tasks leave the active
queue; historical reports retain their evaluated revision and timestamp.

## Operating and architecture

| Document | Purpose |
| --- | --- |
| [Project README](../README.md) | Setup, supported interfaces, configuration, and verification entrypoints. |
| [Runbook](runbook.md) | Operations, recovery, diagnosis, and evaluation procedures. |
| [Architecture](architecture.md) | Runtime components and execution boundaries. |
| [State ownership](architecture/state_ownership.md) | Temporal/Postgres authority, rehydration, retained state, and recovery contracts. |
| [Threat model](architecture/threat_model.md) | Trust boundaries and sandbox/security assumptions. |
| [Temporal compatibility](temporal_compatibility.md) | Qualified server/SDK pair, replay gate, workflow evolution, and rollback. |

## Historical records

- [Phase 4A evidence](archive/phase4a_evidence.md): qualification notes and M29
  wave accounting; old sample gaps and relative ages are historical snapshots.
- [Temporal cutover](archive/temporal_cutover.md): migration and rollback record.
- [Acceptance invariants](archive/acceptance-invariants-verification.md):
  archived verification record.

Archive records do not establish live deployment readiness or authorize changes
to routing, autonomy, security, or deployment permissions.

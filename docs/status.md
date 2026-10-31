# Status

Planning snapshot: **2026-10-31**. This describes repository capabilities and
tracked work, not the health or activation state of a deployed instance.

## Current phase

**Phase 4A — Temporal stabilization and measured reliability.**
**M31 — Proactive Operations and Safe Scheduled Work** is the only active
milestone. M25.6, M26, M28, M28.5, M29, and M30 are complete.

M31 already includes the qualified Temporal compatibility baseline, weekly
read-only dependency reviews and management API, read-only operations dashboard,
opt-in Telegram readiness alerts, task-specific stuck-interaction,
stale-outbox, and terminal-reconciliation alerts, provider-capability alerts,
and opt-in generic webhook delivery.
The remaining work is below.

## Current capabilities

- authenticated API, generic webhook, Telegram, and GitHub-native task intake
- Temporal-owned lifecycle, transactional command outbox, retries, cancellation,
  human waits, recovery, and replay controls
- TaskSpec routing with manual overrides; Codex and Antigravity native workers
  and opt-in OpenRouter execution in isolated Docker workspaces
- sequential task DAGs and opt-in bounded two-node read-only fan-out
- verification, independent review, bounded repair, draft-PR delivery, and
  review-comment repair with retained evidence
- skeptical personal/project memory, compact session state, full-text retrieval,
  versioned ContextEnvelope inspection, and opt-in AgentEvent capture
- dashboard task controls, timelines, artifacts, memory, traces, readiness,
  operational alerts, scheduled-run history, and synchronized PR/CI state
- offline provider reliability advisory and robustness reports; all four
  canonical task-class/mode cells met the sample floor at M29 closeout

## Current Backlog

This is the canonical execution order. GitHub issues hold each slice's detailed
scope and acceptance checklist; the roadmap holds milestone goals and boundaries.
Only the M31 table is the default implementation queue. Maintenance, approval,
and evidence-gated lanes do not silently replace the next M31 slice.

Status meanings: **Ready to scope** = next bounded task; **In progress** = work
underway; **Complete** = merged and its completion condition is met;
**Planned** = ordered follow-up requiring its contract/design;
**Evidence-gated** = eligibility must be proved; **Approval-gated** =
external/security changes require operator approval.
M31-1, M31-2, and M31-3 were completed in [PR #459](https://github.com/natanayalo/code-agent/pull/459),
[PR #461](https://github.com/natanayalo/code-agent/pull/461), and
[PR #463](https://github.com/natanayalo/code-agent/pull/463), respectively.

### M31 implementation queue

| Order | Slice | Status | Completion condition |
| --- | --- | --- | --- |
| 3 | [M31-3: Operator acknowledgment](https://github.com/natanayalo/code-agent/issues/452) | Complete | Authenticated acknowledgment distinct from observed recovery; auditable recurrence semantics and tested dashboard/API controls. |
| 4 | [M31-4: Health-summary and evidence schedules](https://github.com/natanayalo/code-agent/issues/453) | In progress | Health summaries shipped in PR #466; evidence collection is under verification. Prove idempotency, cancellation, recovery, and terminal evidence. |
| 5 | [M31-5: Schedule request and management UX](https://github.com/natanayalo/code-agent/issues/454) | Planned | Select the first request surface; preserve explicit intent and timezone; split natural-language input and dashboard controls into bounded slices. |

M31-3 defines acknowledgment as an auditable action for one degraded transition.
It cancels pending degraded retries on Telegram and webhook, preserves recovery
notifications and delivery evidence, and leaves sends already in flight alone.
Recovery ends the incident; each later degraded transition starts unacknowledged.
The API and dashboard use the existing authenticated single-operator identity;
rollback keeps the additive migration applied unless audit evidence is exported.

The health-summary slice extends the structured schedule API beyond `dependency_review`.
Create/pause/resume/trigger APIs exist; dashboard schedule views are read-only.
Natural-language schedule interpretation and dashboard management are planned,
not shipped capabilities. Scheduled mutation remains disabled.

M31 closes when every supported degraded scenario has actionable degradation and
recovery evidence, notification failure is inspectable without blocking task
reconciliation, and supported read-only schedules are idempotent, inspectable,
cancellable, and leave terminal evidence. Each remaining slice must add its
required tests and operator documentation before being marked complete.

### Maintenance and approval lane

| Item | Status | Next action |
| --- | --- | --- |
| [Security closeout, PR #430](https://github.com/natanayalo/code-agent/pull/430) | Draft / approval-gated | Reconcile the draft with current master; review credential-refresh and isolation evidence plus activation/rollback gates. The PR's historical dependency-alert count is not a current security assessment. |
| [OPS-1: Dependency PR triage](https://github.com/natanayalo/code-agent/issues/455) | Ready to scope | Review current advisories/CI and record a disposition per update; requalify Temporal compatibility before any SDK update. |
| [OPS-2: Required compatibility check](https://github.com/natanayalo/code-agent/issues/457) | External follow-up | Inspect current branch rules; the workflow alone does not prove enforcement. Obtain explicit approval for any setting change. |

As of this snapshot, 13 dependency PRs remain open. Their inventory and
validation/disposition checklist live in the dependency-triage issue; refresh
that inventory when triaging rather than treating this count as live state.

### Evidence-gated follow-up

| Item | Status | Entry and completion conditions |
| --- | --- | --- |
| [M29-F1: Longitudinal robustness refresh](https://github.com/natanayalo/code-agent/issues/456) | Evidence-gated | Verify model-aware cohort age/counts, recompute the existing offline reports at a pinned timestamp, and publish sanitized outcomes. Estimates: around 2026-11-02 for earlier waves and 2026-12-08 for gpt-6-luna; dates alone do not establish eligibility. |
| [M27: Reliability-based autonomy](roadmap.md#m27--reliability-based-autonomy-policy) | Deferred | Requires sufficient repeated evidence and a separately reviewed, reversible policy proposal. A refreshed report does not automatically authorize autonomy. |

## Evidence and limits

- The reviewed M25.6 baseline has 20/20 valid real-worker captures; M28 has
  eight valid provider pairs proving safe memory behavior, not general speedup.
- M29's four advisory cells are qualified at closeout; 45-day longitudinal
  robustness remains partial. Read-only feature acceptance measures completion,
  not independently verified quality. Production routing still uses static
  policy/checked-in metrics and is not refreshed from live outcomes.
- The repository-qualified Temporal baseline is Server 1.24.2 / Python SDK
  1.30.0. Worker Deployment Versioning is disabled; this is not a claim of
  upstream maintenance support.
- The trusted Temporal worker retains Docker authority. Native agents use
  one-shot task containers; a separate sandbox broker remains deferred.
- Full-text retrieval retains synonym gaps; current evidence does not justify
  a vector dependency. Native-agent audit/adapters remain complexity hotspots.
- Python and dashboard CI coverage targets are 90%. Test results and runtime
  health must be verified on the candidate revision, not inferred from this page.

Detailed historical qualification notes are in the
[Phase 4A evidence archive](archive/phase4a_evidence.md). Merged changes belong in
[CHANGELOG.md](../CHANGELOG.md), not in this backlog.

## Future / conditional

Durable child workflows, mutable parallel execution, isolated worktree/patch
reconciliation, an operator-visible agent tree, evidence-backed procedural
skills, checkpoint UX, remote sandbox scaling, deep-scout chaining, and semantic
retrieval remain [conditional directions](roadmap.md#future--conditional-execution-architecture).
They have no committed implementation slot. Auto-merge, auto-deploy, and broad
multi-user platform work remain outside the current product scope.

## Keeping planning current

When a slice starts, update its state here and link its issue/PR. When it merges,
remove it from the active queue and reconcile capability/roadmap statements in
the same closeout; generated merged history stays in CHANGELOG.md. Refresh the
snapshot date when priorities or external backlog observations change. Preserve
historical evidence with its evaluated revision and timestamp rather than
presenting old wave gaps or relative ages as current facts.

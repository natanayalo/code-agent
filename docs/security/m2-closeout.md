# M2 closeout — repository candidate, activation pending

## Identity and milestone state

Audited and implemented from commit
`036bde95c74d517f80c17e6bf32e92d300e3b873` on
`codex/m2-security-hardening`; audit on 24 October and final verification on
25 October 2026, Asia/Jerusalem. Planning's older
commit `2e7e059` was superseded before implementation. Environment: Windows 11
Home 10.0.26300, Ubuntu/WSL2, Docker Desktop `desktop-linux` engine 29.9.2,
approximately 31 GiB RAM and eight logical CPUs.

Focused implementation commits:

- Ingress: `9e5af6612878e87fdb5caf83c1ddd47eaa4a7176`.
- Execution/credentials: `856dab6df9abb072ea9ba2f9ab62a7536f805d2c`.
- Operational evidence: the commit containing this closeout and the other five
  reports, plus `scripts/m2_preservation.ps1`.

| Milestone | State |
| --- | --- |
| Repository implementation | Candidate prepared with code, tests and six security documents; review required |
| Synthetic security validation | Executed tests pass within recorded boundaries; independent-daemon compromise and real provider refresh remain UNVERIFIED |
| Live activation | NOT PERFORMED; Code Agent was stopped and remains unactivated |
| Host-level isolation demonstrated | NO; shared Docker authority remains an architectural risk |
| FamilyFin production approval | NO; synthetic-only authorization and existing production blockers unchanged |
| Full regression suite green | NO; an unrelated timing failure reproduces on the untouched audited base |

## Before and candidate-after exposure

These are repository defaults, not claims about an activated candidate runtime.

| Surface | Audited configuration | Candidate configuration |
| --- | --- | --- |
| PostgreSQL 5432 | Wildcard host publication | `127.0.0.1:5432`; retained for local tooling |
| Temporal 7233 / UI 8080 | Wildcard | Loopback only; UI port conflict with existing Adminer must be resolved by Code Agent-only override |
| API 8000 / dashboard 3000 | Wildcard | Loopback only; no dashboard Tailscale route |
| Phoenix 6006/4317 | Optional wildcard publications | Optional loopback publications |
| ngrok management 4040 | Wildcard and local request inspection | Loopback, inspection disabled |
| Public ngrok target | Whole `api:8000` | Canonical POST webhook only through pinned non-root Nginx |
| Proxy/control-plane networking | Direct ngrok/API adjacency | Edge network → proxy → API-only internal upstream; proxy has no DB-network attachment |
| Telegram operator gate | Optional secret, no user/chat gate | Explicit enablement, strong secret and both numeric identity lists; direct private messages only |
| Worker Docker authority | Full shared socket | Still full shared socket; no authority/daemon migration was applied |

Actual baseline had no Code Agent container or application listener. Existing
FamilyFin synthetic/restore proxies remained on localhost 8090/8091, M0 on 8088
and Adminer on 8080. Serve continued routing private HTTPS to FamilyFin on 8090.

## Changes and findings

P0 repository remediation addresses whole-API tunneling, optional Telegram
authentication, missing operator authorization and unnecessary wildcard
publications. P1 addresses writable task authentication, broad source mounts and
account fallback, legacy root/default-network execution, body limits, staging
path aliases, cleanup and secret-bearing inspection/registration diagnostics.
See [m2-audit.md](m2-audit.md) for each finding's severity, confidence,
prerequisites, impact and evidence. Configuration weaknesses are not presented as
confirmed live exploitation.

The only added infrastructure is a digest-pinned minimal ingress proxy. Python
application dependencies, database schema and default cloud/local model settings
are unchanged. Codex, Gemini/Antigravity adapters remain behind their existing
execution interfaces. Public CA support is preserved; private auth/environment
variants are excluded from image contexts.

Verification includes 3,152 passing unit/worker tests; 243 passing focused
security tests; 75 passing ingress/config/registration cases; six passing real
container/restore tests; a separate concurrent duplicate + restored-state check;
the API/Temporal/shell workflow smoke; 51 launch-error/execution checks;
90.64% combined branch-aware coverage; and 100% coverage of 260 changed
executable security lines. Exact evidence, initial failures, corrected reruns,
the baseline timing failure and unverified categories are recorded in
[m2-validation.md](m2-validation.md). Do not add these overlapping counts into a
unique-test total or claim that the full integration suite passed.

Normal pre-commit hooks passed, including secret scanning, schema validation,
shellcheck, Ruff, formatting, mypy and size policy; no checks were bypassed.

Existing API/dashboard authentication, approvals/replay, generic webhook,
Telegram progress/final responses, durable delivery/session reuse and provider
contracts have regression coverage. Real authenticated provider refresh, the
running desktop UI and independent-network access remain activation gates.
Provider auth copies are readable by the selected CLI; immutability is not
confidentiality from that CLI and does not reduce the source token's account scope.

## Live state, rollback and residual risks

Residual risks, in priority order:

1. **HIGH — shared daemon authority:** a compromised trusted worker can administer
   unrelated containers/storage. Dedicated VM or separate host acceptance remains
   necessary for the stronger isolation claim.
2. **HIGH — activation evidence absent:** independent external positive/negative
   access checks, operator enrollment and actual image/configuration acceptance
   must precede live ingress approval.
3. **MEDIUM — provider compatibility and scope:** real token refresh with immutable
   auth files and metadata-only repository-token scope checks remain unverified.
4. **MEDIUM — incomplete global regression gate:** the unrelated M29 timing failure
   reproduces on the base; ownership and follow-up are explicit below.
5. **Separate financial-production gate:** M2 does not approve FamilyFin real data,
   recovery custody or production use.

No ngrok tunnel or Telegram registration was changed; no live credentials were
read, rotated or staged. No production container, Docker daemon, firewall,
scheduled task, backup destination, recovery material or Tailscale rule was
changed. Synthetic fixtures mount only assigned test paths/volumes, never
FamilyFin storage. Repeated metadata preservation checks pass for all 12 original
containers and Serve. They establish unchanged inspected configuration/state,
not an audit of financial records or host compromise resistance.

The prepared activation/rollback package is in
[m2-operations.md](m2-operations.md). It requires private deployment configuration,
verified provider refresh, protected Code Agent state backup, task drain,
resolved localhost port conflicts, exact images/commands and explicit approval
before service startup or ingress cutover. Reverting M2 must not silently
reintroduce whole-API public access or optional Telegram authentication. Prefer
disabled public ingress while restoring reviewed private services/state.

The trusted worker's socket can administer FamilyFin's engine resources if that
worker is compromised. This is unresolved, not solved by child hardening. Before
sensitive coexistence, follow the separate Ubuntu/independent-engine VM design:
Hyper-V after an approved Windows Pro upgrade, guest-local control plane/storage,
no host drive/socket sharing and externally enforced private-network restrictions.
No VM/hypervisor upgrade or workload migration was performed. A separate Linux
machine is the fallback if host maintenance is declined.

FamilyFin's real-data readiness is not advanced to approval by M2. Shared-host
authority, exact-image security decisions, recovery/key custody and household
production authorization remain separate decisions. The declined 25 October
backup observation was neither verified nor reintroduced as a prerequisite.

Follow-up owner: Code Agent maintainer. Resolve the existing M29 test's
wall-clock cutoff in a separate change before claiming a clean full suite.
No merge waiver, deployment approval or FamilyFin real-data permission is issued
by this closeout.

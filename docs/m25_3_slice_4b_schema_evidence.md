# M25.3 Slice 4B — Schema Cleanup Evidence

## Decision

The snapshot, PostgreSQL migration round trip, restore procedure, and retained
legacy-image compatibility all passed on 2026-07-27 UTC. The schema-cleanup PR
is safe to review. The sole live Compose database was not migrated while this
evidence was collected.

## Pre-migration snapshot

| Field | Recorded value |
| --- | --- |
| Source Alembic revision | `20260720_0046` |
| Active tasks | `0` |
| Terminal tasks | `115` (`79` completed, `36` failed) |
| Tasks with `next_attempt_at` | `53` |
| Non-null task lease owner/expiry | `0` / `0` |
| Worker nodes | `24`, all `current_load = 0` |
| Snapshot format | PostgreSQL custom format (`pg_dump -Fc`) |
| Ignored local artifact | `artifacts/m25_3_slice_4b/20260727T215413Z/pre_migration.dump` |
| Snapshot size | `1.2 MB` |
| SHA-256 | `6ed54402661dbc21f8fcde8c070f966f3254d45c4ac44f8ac73096f36fbad376` |
| Restore-list validation | Passed |

The dump is intentionally ignored by Git because persisted task data may be
sensitive. Retain it locally through deployment acceptance.

## Rollback artifacts

| Service | Tag | Verified image digest |
| --- | --- | --- |
| API | `code-agent-api:m25.3-legacy-lkg-20260727` | `sha256:060981998c74a61eb799f9babaa26d28d9253b3d98248ada78edbf2484dbe4b3` <!-- gitleaks:allow --> |
| Worker | `code-agent-worker:m25.3-legacy-lkg-20260727` | `sha256:8b51244ad5ff6bfca228c129ffaa033fdefc86420837d907fbab062e9cee8a46` <!-- gitleaks:allow --> |
| Migrate | `code-agent-migrate:m25.3-legacy-lkg-20260727` | `sha256:af052c9c20fb361bc63dd29f44e8a621219ea3fb55316bbc40cfa21884d2e27a` <!-- gitleaks:allow --> |
| Dashboard | `code-agent-dashboard:m25.3-legacy-lkg-20260727` | `sha256:20da0c1214ae7c6ef3b09daab7d6ba5f5f63f45155483a60c02f896ed7603bd2` <!-- gitleaks:allow --> |

## Disposable PostgreSQL rehearsal

1. Restored the snapshot into `code_agent_m25_3_slice_4b_verify`; revision,
   terminal task counts, all 53 retry timestamps, and 24 worker rows matched.
2. Upgraded to `20260728_0047`. The three task lease columns, their two
   indexes, WorkerNode `current_load`, and its two constraints were absent.
3. Retained task attempt/policy totals and worker capacity/failure totals
   matched, and a new task write succeeded.
4. Downgraded to `20260720_0046`. All retired columns, indexes, and constraints
   were recreated; dropped task values were `NULL` and worker loads were `0`,
   confirming that downgrade restores shape rather than data.
5. Re-upgraded to `20260728_0047` successfully.
6. Restored a fresh second database from the same dump. The retained
   `code-agent-migrate:m25.3-legacy-lkg-20260727` image reported
   `20260720_0046 (head)`.
7. Removed both disposable databases after the checks passed.

## Automated verification

- Focused model, migration, and worker-node suite: `25 passed`.
- Docker-backed vertical-slice smoke: `1 passed`.
- Exact configured unit/worker CI gate: `1,710 passed`; command exited zero
  with 79.51% raw branch coverage (80% at the configured report precision).
- Full integration suite with host process and Docker access: `290 passed`,
  `1 skipped` because `CODE_AGENT_TEST_POSTGRES_URL` was not configured.
- Full pre-commit suite and `git diff --check`: passed.

The stricter policy command completed all `1,703` unit tests but reported
89.22%, below the documented 90% repository-wide target. This schema slice
fully covers its changed behavior; the repository-wide policy/CI threshold
mismatch remains follow-up work rather than expanding this PR into unrelated
coverage changes.

## Deployment and rollback order

Deployment must run in a maintenance window with no active tasks:

1. stop API and worker services
2. retain and revalidate the recorded pre-4B snapshot checksum
3. apply Alembic revision `20260728_0047`
4. deploy the matching application images, start services, and verify
   readiness plus task/run projections

If rollback is required:

1. stop API and worker services and block new submissions
2. drop and recreate only the code-agent application database
3. restore the recorded custom-format snapshot with `pg_restore`
4. deploy the four `m25.3-legacy-lkg-20260727` images and matching configuration
5. verify Alembic revision `20260720_0046`, expected task/run counts, API
   readiness, and worker health before accepting submissions

Do not use Alembic downgrade alone as the data-recovery procedure.

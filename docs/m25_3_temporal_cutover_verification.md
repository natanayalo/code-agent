# M25.3 Slice 2 — Temporal Production Cutover Verification

## Automated evidence

- Runtime defaults, explicit legacy fallback, timestamp parsing, worker retry exhaustion, submission outage, recovery, and dashboard metrics are covered by the focused unit and integration suites.
- The existing Temporal integration suite covers workflow lifecycle, interaction signals, cancellation, sequential DAGs, bounded fan-out, and replay compatibility.
- The Slice 2 implementation and automated verification are complete. Compose
  scenario results below must be recorded against the deployment that begins
  Slice 3 in the [Temporal observation evidence ledger](m25_3_observation_ledger.md);
  this document does not represent unperformed manual scenarios as passed.

## Operational scenarios

Run these against the production-like Compose stack before starting the
observation window. Record the deployment image, timestamp, task IDs, and
outcome in the [Temporal observation evidence ledger](m25_3_observation_ledger.md).

1. Submit an authenticated Compose task and confirm its complete lifecycle.
2. Exercise approval, clarification, and permission resume through Temporal signals.
3. Cancel a running provider activity and inspect terminal projection evidence.
4. Restart the worker during an activity and confirm Temporal recovery.
5. Stop Temporal: verify API reads remain available and submissions return 503.
6. Run a sequential DAG task.
7. Run the two-node read-only fan-out task.
8. Replay an older Temporal workflow history after deployment.
9. Run the full Python suite and pre-commit.
10. Configure mismatched API/worker runtime values and confirm the mismatch is visible.
11. With Temporal unavailable, confirm `/tasks` rejects while task inspection stays available.
12. Restore Temporal and confirm a new submission succeeds without API restart.
13. Restart the worker and reconcile Temporal and Postgres terminal state.
14. Replay existing M25.1 and M25.2 workflow histories.

## Cutover procedure

1. Deploy with `CODE_AGENT_EXECUTION_RUNTIME=temporal` and set immutable `TEMPORAL_ONLY_CUTOVER_AT` to the UTC deployment timestamp.
2. Run `scripts/up.sh`; it always starts `temporal` and `temporal-ui` with the API, worker, and dashboard.
3. Copy the [Temporal observation evidence ledger](m25_3_observation_ledger.md)
   to an immutable, release-specific evidence record. Complete and attach the
   evidence for all scenarios there. Start the 7-day active soak only after all
   14 scenarios pass.
4. Roll back with the last known-good image and compatible schema/configuration; do not use legacy as an automatic runtime fallback.

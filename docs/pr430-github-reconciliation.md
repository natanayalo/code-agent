# PR #430 split: GitHub reconciliation

Depends on CI ordering. Unmatched delivery IDs persist with a null task_id, a pending reconciliation marker using the existing schema. Redelivery retries matching until both task and worker metadata exist, then atomically applies metadata/timeline and attaches the task to the inbound claim. Completed deliveries deduplicate normally; existing null-task ignored records can also reconcile. No raw event payload or credential is persisted.

Pending responses remain HTTP 200 with status=pending; operators must redeliver from GitHub after metadata persists. This PR does not introduce an automatic event poller or retain payloads, so a delivery that is never redelivered remains pending. That limit is explicit rather than permanently consuming it as a completed duplicate. No migration; revert to roll back. This forms part of the PR #430 split.

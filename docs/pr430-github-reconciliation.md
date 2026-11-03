# PR #430 split: GitHub reconciliation

Depends on CI ordering. Unmatched delivery IDs persist with a null task_id, a pending reconciliation marker using the existing schema. Redelivery retries matching until both task and worker metadata exist, then atomically applies metadata/timeline and attaches the task to the inbound claim. Completed deliveries deduplicate normally; existing null-task ignored records can also reconcile. No raw event payload or credential is persisted.

Pending responses remain HTTP 200 with status=pending; operators must redeliver from GitHub after metadata persists. This PR does not introduce an automatic event poller or retain payloads, so a delivery that is never redelivered remains pending. That limit is explicit rather than permanently consuming it as a completed duplicate. No migration; revert to roll back. This forms part of the PR #430 split.

Delivery transactions for the same repository are serialized with PostgreSQL transaction advisory locks; SQLite serializes writers. On PostgreSQL, a busy delivery retries asynchronously 80 times at 100 ms intervals without retaining a database connection between attempts. If contention persists, the webhook returns HTTP 503 and the operator must redeliver the failed delivery; GitHub does not automatically redeliver failed webhooks.

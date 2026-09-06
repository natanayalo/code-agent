# Proposed verifier read-only audit correction

Status: explicitly approved, implemented, and verified through live service ingress.

## Reproduction

Live task `4b37871e-928f-40e4-ba34-5ccc7e1b221d` created `qa-acceptance.txt` and
passed deterministic checks. The independent verifier then reported a read-only
violation for that inherited edit. This replaced an authentication error and
prevented the existing provider fallback.

A local unit reproducer initialized a temporary Git repository, added an untracked
file before the verifier began, and mocked the executor to return success without
performing any operation. `run_native_agent` still returned `failure` with
`READ_ONLY_VIOLATION` and the inherited filename. No real worker ran on the host.

## Smallest proposed change

In `workers/native_agent_runner.py`, distinguish the task's overall Git diff from
mutations attributable to an individual read-only invocation. Capture a trusted
before/after workspace snapshot around that invocation, including file content and
relevant entry metadata. Compare snapshots for the read-only decision, while
retaining task-level diff artifacts. Merely subtracting filename sets is insufficient:
a verifier could modify an already-dirty file without changing the filename set.

Keep read-only mounts and permissions intact. Do not disable independent
verification, change credentials, add a provider-selection option, or forgive
actual verifier mutations. If baseline inspection cannot complete reliably, fail
explicitly rather than treating missing evidence as an unchanged workspace.

Regression coverage must show: inherited tracked and untracked edits are allowed
when unchanged; modifications to an already-dirty file, new files, deletion,
renames, and relevant metadata changes are rejected; ordinary execution artifacts
remain available; genuine provider errors retain their classification and the
existing fallback path. Add integration coverage through independent verification.

The implementation uses descriptor-relative traversal with no-follow opens,
content hashes, entry types, modes, and symlink targets. It reuses existing audit
runtime exclusions at every directory depth and excludes broker SQLite scratch
files. Read-only result fields describe only invocation changes; the original task
diff artifact remains available. Snapshot errors fail closed before execution or
on result collection, including timeout and startup-error paths. The exact provider
message `authentication required` now retains `provider_auth` classification so
the existing fallback can run. No provider override was introduced.

Snapshot helper coverage: 100% of lines and branches; 22 focused cases passed.
The integration regression confirms both fallback on unchanged inherited edits
and rejection of real verifier mutations.

Final verification: 2,411 unit/worker tests and 361 integration tests passed;
combined coverage is 90.09%. The worker was rebuilt at `c9c23264`, and one bounded
task (`4cfe547d-11e9-41eb-bade-77533c012f9a`) completed with required verification,
broker delivery evidence, and matching API/timeline/Temporal results. The remote
branch commit and exact file bytes were independently checked. See
`docs/acceptance-invariants-verification.md`. PR #383 remains unmerged pending
operator review.

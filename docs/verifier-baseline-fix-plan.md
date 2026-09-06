# Proposed verifier read-only audit correction

Status: awaiting explicit approval for sandbox-behavior changes under AGENTS.md.

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

After focused tests and the full coverage gate pass, rebuild the local trial worker
and submit one bounded dummy-repository task. Require independently verified file
content, pushed commit, broker delivery evidence, and matching API/timeline/Temporal
completion. Keep PR #383 draft and unmerged until this live happy path succeeds.

---
name: e2e-qa
description: Run the live webhook-to-Temporal-to-worker happy path against a disposable repository, checking terminal task and sandbox evidence.
---

# Live end-to-end QA

Use this for affected infrastructure/orchestration flows under the
[AGENTS.md test gate](../../../AGENTS.md#testing-policy). The helper submits
through /webhook, follows Temporal-backed task execution via /tasks/{task_id},
and checks qa-hello.txt in the resulting sandbox workspace.

## Target and inputs

Inspect [the helper](scripts/run_e2e_qa.py) before running it. It recreates
`dummy_repo` under the configured workspace root and invokes a real worker;
use a disposable QA target, not an existing project directory.

- Start the intended local QA stack with scripts/up.sh. The default API is
  http://127.0.0.1:8000; CODE_AGENT_API_URL overrides it.
- Set CODE_AGENT_API_SHARED_SECRET in the environment or .env without printing it.
- CODE_AGENT_QA_REPO_KEY defaults to qa-dummy; the API/worker allowlist must map
  it to the helper's disposable repository.
- CODE_AGENT_WORKSPACE_ROOT must match the stack. The helper defaults to
  ~/code-agent-workspaces; keep the path visible for Antigravity.
- CODE_AGENT_WORKER_OVERRIDE selects the worker (default: antigravity).
  CODE_AGENT_QA_TASK_TEXT can change the task; inspect the helper's acceptance
  checks before changing the expected artifact.

From the repository root:

```bash
.venv/bin/python .agents/skills/e2e-qa/scripts/run_e2e_qa.py
```

Report the task ID, terminal result, verification/artifact evidence, and any
failure. A submitted task or healthy API alone is not a passing worker smoke.
The helper also has bounded DAG/fan-out and delivery companions in scripts/;
read the relevant helper and its inputs only when the affected flow requires it.

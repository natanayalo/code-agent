---
name: scout-e2e-qa
description: Run a live read-only scout task through webhook and Temporal execution, checking terminal state and absence of the prohibited test mutation.
---

# Read-only scout end-to-end QA

Use for read-only routing/constraint/verification changes. Inspect
[the helper](scripts/run_scout_e2e_qa.py) and follow
[AGENTS.md](../../../AGENTS.md#testing-policy). The helper recreates dummy_repo,
submits task_type: scout through /webhook, polls the Temporal-backed task, and
checks that qa-hello.txt was not created in the sandbox.

## Target and inputs

- Use the intended disposable QA stack and repository; setup removes/recreates
  dummy_repo under CODE_AGENT_WORKSPACE_ROOT.
- CODE_AGENT_API_URL defaults to http://127.0.0.1:8000.
- CODE_AGENT_API_SHARED_SECRET is required in the environment or .env.
- CODE_AGENT_E2E_DUMMY_REPO_KEY defaults to dummy-repo; map it to the disposable
  repository in the existing API/worker allowlist.
- Set CODE_AGENT_WORKSPACE_ROOT explicitly to match the stack. This older helper
  falls back to ~/.code-agent/workspaces; use a visible configured root for
  Antigravity rather than assuming it shares the mutation helper's default.
- CODE_AGENT_WORKER_OVERRIDE selects the worker (default: antigravity).

From the repository root:

```bash
.venv/bin/python .agents/skills/scout-e2e-qa/scripts/run_scout_e2e_qa.py
```

Report terminal task status and observed workspace/diff evidence. The helper's
absence check is a scoped smoke, not proof that every possible read-only
violation is prevented; retain the affected unit/integration coverage too.

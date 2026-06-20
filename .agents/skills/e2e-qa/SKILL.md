---
name: e2e-qa
description: Learn how to perform an End-to-End (E2E) QA via the webhook API, validating the full system graph (API -> Orchestrator -> Worker) using a dummy repository.
---

# End-to-End (E2E) QA

When validating major architecture changes, database schema migrations, or orchestrator state machine updates, you need to test the entire system from the outside in, just like a real user sending a request from Telegram or a dashboard.

This skill provides an automated E2E QA script that:
1. Creates a local Git "dummy repo" (`dummy_repo`) to safely test edits without risking real codebases.
2. Uses the local FastAPI server (`/webhook`) to schedule a task.
3. Polls the `/tasks/{task_id}` endpoint while the LangGraph Orchestrator routes and processes the task.
4. Waits for completion and verifies the actual Docker sandbox output on disk to ensure the AI's changes were successfully applied to the workspace.

## Prerequisites

Before running this script, your local environment must be fully running via the project startup script:
```bash
scripts/up.sh
```
Ensure the API is listening on `http://127.0.0.1:8000` (which is the default).

## Instructions

1. **Navigate to the root of the repository.**
   ```bash
   cd /path/to/code-agent
   ```

2. **Run the E2E QA Script.**
   ```bash
   poetry run python .agents/skills/e2e-qa/scripts/run_e2e_qa.py
   ```

3. **Check the Output.**
   - The script will announce `[+] Task ingested successfully. Task ID: <uuid>`.
   - It will poll the status (e.g. `pending`, `in_progress`, `completed`).
   - Once finished, it looks inside your local `~/.code-agent/workspaces/` (or whatever `CODE_AGENT_WORKSPACE_ROOT` is set to in `.env`) to find the artifact `qa-hello.txt` created by the AI.
   - If the artifact exists and contains the correct text, the QA passes!

## Customizing the QA

You can edit `.agents/skills/e2e-qa/scripts/run_e2e_qa.py` to test different worker profiles (e.g. Codex instead of Antigravity) by changing the `worker_override` in the JSON payload, or to test different failure scenarios (like providing an invalid repository URL).

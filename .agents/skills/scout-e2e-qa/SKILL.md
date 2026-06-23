---
name: scout-e2e-qa
description: Learn how to perform an End-to-End (E2E) QA specifically for read-only scout tasks via the webhook API, validating the full system graph using a dummy repository.
---

# Scout End-to-End (E2E) QA

When validating architecture changes related to read-only mode, task constraints, gitignore hardening, or orchestrator verification logic, you need to test the entire system with a read-only scout task.

This skill provides an automated E2E QA script that:
1. Creates a local Git "dummy repo" (`dummy_repo`) to safely test edits without risking real codebases.
2. Uses the local FastAPI server (`/webhook`) to schedule a task with `task_type: scout` constraints.
3. Polls the `/tasks/{task_id}` endpoint while the LangGraph Orchestrator routes and processes the task.
4. Waits for completion and verifies the actual Docker sandbox output on disk to ensure the AI's changes were successfully applied to the workspace. Specifically, it asserts that the `qa-hello.txt` file was **not** created, verifying the read-only constraint.

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

2. **Run the Scout E2E QA Script.**
   ```bash
   poetry run python .agents/skills/scout-e2e-qa/scripts/run_scout_e2e_qa.py
   ```

3. **Check the Output.**
   - The script will announce `[+] Task ingested successfully. Task ID: <uuid>`.
   - It will poll the status (e.g. `pending`, `in_progress`, `completed`).
   - Once finished, it looks inside your local `~/.code-agent/workspaces/` (or whatever `CODE_AGENT_WORKSPACE_ROOT` is set to in `.env`) to find the dummy workspace.
   - If the task finishes successfully, and no code was mutated (e.g., `qa-hello.txt` correctly NOT FOUND), the QA passes!

## Customizing the QA

You can edit `.agents/skills/scout-e2e-qa/scripts/run_scout_e2e_qa.py` to test different worker profiles (e.g. Codex instead of Antigravity) by changing the `worker_override` in the JSON payload, or to test different failure scenarios (like providing an invalid repository URL).

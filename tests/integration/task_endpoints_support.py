"""Shared helpers and fixtures for task endpoint integration tests."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from workers import Worker, WorkerRequest, WorkerResult

DEFAULT_SHARED_SECRET = "a" * 32  # gitleaks:allow


class StaticWorker(Worker):
    """Worker double that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


def _run_one_queued_task(client: TestClient) -> None:
    """Claim one queued task and execute it through the worker service."""
    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=60)
    assert claim is not None
    asyncio.run(service.run_queued_task(task_id=claim.task_id, worker_id="test-worker"))


def _default_worker() -> StaticWorker:
    return StaticWorker(
        WorkerResult(
            status="success",
            summary="Created note.txt and retained the workspace for inspection.",
            budget_usage={"iterations_used": 2, "tool_calls_used": 1},
            commands_run=[
                {
                    "command": "printf 'done\\n' > note.txt",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout_artifact_uri": "artifacts/stdout.log",
                    "stderr_artifact_uri": "artifacts/stderr.log",
                }
            ],
            files_changed=["note.txt"],
            artifacts=[
                {
                    "name": "workspace",
                    "uri": "/tmp/workspace-task-44-1234",
                    "artifact_type": "workspace",
                }
            ],
            next_action_hint="inspect_workspace_artifacts",
        )
    )

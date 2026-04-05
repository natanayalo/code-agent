"""End-to-end integration tests for the vertical slice."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from db.models import Task, WorkerRun
from orchestrator.execution import TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
from workers import CodexCliWorker
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeStep

# Use a test-specific DB
TEST_DATABASE_URL = "sqlite:///test_vertical_slice.sqlite"


class _ScriptedAdapter(CliRuntimeAdapter):
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)

    def next_step(self, messages, **kwargs) -> CliRuntimeStep:
        if not self._steps:
            return CliRuntimeStep(kind="final", final_output="Done.")
        return self._steps.pop(0)


@pytest.fixture
def session_factory():
    """Create a test session factory with an initialized schema."""
    engine = create_engine_from_url(TEST_DATABASE_URL)
    # Note: For real integration, we'd run migrations.
    # For this E2E test, we'll manually create tables
    from db.models import Base

    Base.metadata.create_all(engine)

    factory = create_session_factory(engine)
    yield factory

    # Cleanup
    Base.metadata.drop_all(engine)
    if os.path.exists("test_vertical_slice.sqlite"):
        os.remove("test_vertical_slice.sqlite")


@pytest.mark.anyio
async def test_vertical_slice_e2e_happy_path(session_factory, tmp_path: Path):
    """The full stack should ingest a task, run it in a sandbox, and persist the result."""
    # 1. Setup real components with mocked turns
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="echo 'hello world' > hello.txt",
                final_output=None,
            ),
            CliRuntimeStep(
                kind="final",
                final_output="Successfully created hello.txt.",
                tool_name=None,
                tool_input=None,
            ),
        ]
    )

    from sandbox import DockerShellCommandResult, DockerShellSession

    class _GitMockingSession:
        def __init__(self, container):
            self._real = DockerShellSession(container)

        def execute(self, command, **kwargs):
            if "git status" in command:
                return DockerShellCommandResult(
                    command=command, output=" M hello.txt\0", exit_code=0, duration_seconds=0.1
                )
            return self._real.execute(command, **kwargs)

        def close(self):
            self._real.close()

    worker = CodexCliWorker(
        runtime_adapter=adapter, session_factory=lambda container: _GitMockingSession(container)
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    # 2. Ingest a task
    # We use a dummy repo that exists on the filesystem for cloning
    repo_path = tmp_path / "dummy_repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo", encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init", "--initial-branch=master"], cwd=repo_path, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True)

    task_text = "Create hello.txt in the dummy repo"
    repo_url = f"file://{repo_path.resolve()}"

    from orchestrator.execution import TaskSubmission

    submission = TaskSubmission(task_text=task_text, repo_url=repo_url, branch="master")

    # Create the task first
    snapshot, persisted = service.create_task(submission)
    task_id = snapshot.task_id

    assert task_id is not None

    # Submit the task for background execution
    await service.submit_task(submission, persisted)

    # 3. Wait for the task to complete
    from repositories.session import session_scope

    MAX_WAIT = 30
    elapsed = 0
    while elapsed < MAX_WAIT:
        with session_scope(session_factory) as session:
            stmt = select(Task).where(Task.id == task_id)
            result = session.execute(stmt)
            task = result.scalar_one_or_none()

            if task and task.status in ("completed", "failed", "error"):
                break

        await asyncio.sleep(1)
        elapsed += 1

    # 4. Verify the outcome
    with session_scope(session_factory) as session:
        stmt = select(Task).where(Task.id == task_id)
        result = session.execute(stmt)
        task = result.scalar_one_or_none()

        assert task is not None
        assert task.status == "completed"

        # Verify WorkerRun persistence
        stmt_run = select(WorkerRun).where(WorkerRun.task_id == task_id)
        result_run = session.execute(stmt_run)
        run = result_run.scalar_one_or_none()

        assert run is not None
        assert run.status == "success"
        assert "Successfully created hello.txt" in run.summary
        assert len(run.commands_run) == 1
        assert run.files_changed_count == 1
        assert "hello.txt" in run.files_changed

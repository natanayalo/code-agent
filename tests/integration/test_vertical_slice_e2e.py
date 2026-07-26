"""End-to-end integration tests for the vertical slice."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner
from temporalio.worker import Worker as TemporalWorker

from db.models import Base, Task, TaskTimelineEvent, WorkerRun
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from repositories import create_engine_from_url, create_session_factory, session_scope
from sandbox import DockerShellCommandResult, DockerShellSession
from workers import CodexCliWorker
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeStep


def _run_git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _docker_available() -> bool:
    try:
        docker_info = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            text=True,
        )
        return docker_info.returncode == 0
    except FileNotFoundError:
        return False


class _ScriptedAdapter(CliRuntimeAdapter):
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)

    def next_step(self, messages, **kwargs) -> CliRuntimeStep:
        if not self._steps:
            return CliRuntimeStep(kind="final", final_output="Done.")
        return self._steps.pop(0)


@pytest.fixture
def session_factory(tmp_path: Path):
    """Create a test session factory with an initialized schema."""
    database_path = tmp_path / "test_vertical_slice.sqlite"
    engine = create_engine_from_url(f"sqlite:///{database_path}")
    # Note: For real integration, we'd run migrations.
    # For this E2E test, we'll manually create tables
    Base.metadata.create_all(engine)

    factory = create_session_factory(engine)
    yield factory

    # Cleanup
    Base.metadata.drop_all(engine)


@pytest.mark.anyio
async def test_vertical_slice_e2e_happy_path(session_factory, tmp_path: Path, monkeypatch):
    """The full stack should ingest a task, run it in a sandbox, and persist the result."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

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

    class _GitMockingSession:
        def __init__(self, container, *, secrets=None):
            self._real = DockerShellSession(container, secrets=secrets)

        def execute(self, command, **kwargs):
            if "status --porcelain=v1 -z --untracked-files=all" in command:
                return DockerShellCommandResult(
                    command=command, output="?? hello.txt\0", exit_code=0, duration_seconds=0.1
                )
            return self._real.execute(command, **kwargs)

        def close(self):
            self._real.close()

    worker = CodexCliWorker(
        runtime_adapter=adapter,
        session_factory=lambda container, **kwargs: _GitMockingSession(container, **kwargs),
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    # 2. Ingest a task
    # We use a dummy repo that exists on the filesystem for cloning
    repo_path = tmp_path / "dummy_repo"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo", encoding="utf-8")

    _run_git(["git", "init", "--initial-branch=master"], cwd=repo_path)
    _run_git(["git", "add", "."], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Codex",
            "-c",
            "user.email=codex@example.com",
            "commit",
            "-m",
            "Initial commit",
        ],
        cwd=repo_path,
    )

    task_text = "Create hello.txt in the dummy repo"
    repo_url = f"file://{repo_path.resolve()}"

    submission = TaskSubmission(task_text=task_text, repo_url=repo_url, branch="master")

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        snapshot, _ = service.create_task(submission)
        task_id = snapshot.task_id
        assert snapshot.orchestration_runtime == "temporal"

        activities = TaskExecutionActivities(service=service)
        temporal_worker = TemporalWorker(
            environment.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.select_next_node,
                activities.select_next_node_v2,
                activities.merge_node_wave,
                activities.fail_node_permission_escalation,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.run_decomposed_node,
                activities.request_permission_escalation,
                activities.resolve_permission_escalation,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )
        async with temporal_worker:
            dispatcher = TemporalCommandDispatcher(
                client=environment.client,
                session_factory=session_factory,
            )
            await dispatcher.dispatch_pending()
            handle = environment.client.get_workflow_handle(f"task-{task_id}")
            workflow_result = await handle.result()

        assert workflow_result["status"] == "completed"

    # 4. Verify the outcome
    with session_scope(session_factory) as session:
        stmt = select(Task).where(Task.id == task_id)
        result = session.execute(stmt)
        task = result.scalar_one_or_none()

        assert task is not None
        assert task.status == "completed"
        assert task.orchestration_runtime.value == "temporal"

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

        timeline = (
            session.execute(
                select(TaskTimelineEvent)
                .where(TaskTimelineEvent.task_id == task_id)
                .order_by(TaskTimelineEvent.sequence_number)
            )
            .scalars()
            .all()
        )
        assert timeline
        first_sequence = timeline[0].sequence_number
        assert [event.sequence_number for event in timeline] == list(
            range(first_sequence, first_sequence + len(timeline))
        )
        assert timeline[-1].event_type.value == "task_completed"

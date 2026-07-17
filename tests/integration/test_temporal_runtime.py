from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from temporalio.client import Client, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from db.enums import ExecutionPlanNodeStatus, TimelineEventType
from db.models import Base, Task, TaskTimelineEvent, TemporalTaskState, WorkerRun
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.execution_types import InteractionResponse
from orchestrator.state import OrchestratorState, VerificationReport, VerificationReportItem
from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.queues import CODEX_EXECUTION_TASK_QUEUE
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from repositories import (
    ExecutionPlanRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from sandbox import DockerShellCommandResult, DockerShellSession
from workers import CodexCliWorker, WorkerResult
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


class _GitMockingSession:
    def __init__(self, container, *, secrets=None):
        self._real = DockerShellSession(container, secrets=secrets)

    def execute(self, command, **kwargs):
        if "status --porcelain=v1 -z --untracked-files=all" in command:
            return DockerShellCommandResult(
                command=command,
                output="?? hello.txt\0",
                exit_code=0,
                duration_seconds=0.1,
            )
        return self._real.execute(command, **kwargs)

    def close(self):
        self._real.close()


class _PermissionEscalationWorker:
    """Return an escalation once, then succeed after the operator grants it."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request, *, system_prompt=None) -> WorkerResult:
        self.calls += 1
        if self.calls == 1:
            return WorkerResult(
                status="failure",
                summary="Need workspace_write permission.",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            )
        return WorkerResult(status="success", summary="Completed after permission grant.")


@pytest.mark.anyio
async def test_temporal_runtime_startup_failure_marks_task_failed(session_factory, monkeypatch):
    """A Temporal startup failure must leave visible terminal task evidence."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Temporal unavailable"))

    async def unavailable_client():
        raise ConnectionError("Temporal is unavailable")

    async def skip_backoff(_: float) -> None:
        return None

    monkeypatch.setattr(service, "_get_temporal_client", unavailable_client)
    monkeypatch.setattr("orchestrator.execution.asyncio.sleep", skip_backoff)

    await service.start_temporal_workflow(snapshot.task_id)

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        assert "Temporal workflow startup failed" in task.last_error
        events = (
            session.execute(
                select(TaskTimelineEvent).where(TaskTimelineEvent.task_id == snapshot.task_id)
            )
            .scalars()
            .all()
        )
        assert [event.event_type for event in events].count(TimelineEventType.WORKER_ERROR) == 1


def test_temporal_snapshot_reconciles_operator_approval(session_factory):
    """A paused Temporal snapshot must reflect the operator's approval decision."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Approve this task"))

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        task.constraints = {"approval": {"status": "approved"}}
        TaskTimelineRepository(session).create_next_for_attempt(
            task_id=snapshot.task_id,
            attempt_number=0,
            event_type=TimelineEventType.APPROVAL_GRANTED,
            message="Approved by operator.",
        )
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id,
            state={
                "task": {"task_id": snapshot.task_id, "task_text": "Approve this task"},
                "approval": {"required": True, "status": "pending"},
                "timeline_persisted_count": 0,
            },
        )

    state = TaskExecutionActivities(service=service)._get_current_state(snapshot.task_id)

    assert state.approval.status == "approved"
    assert state.timeline_persisted_count == 1


@pytest.mark.anyio
async def test_temporal_activity_failure_projects_terminal_task(session_factory):
    """An exhausted workflow activity must not leave the product task pending."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Temporal activity failure"))

    await TaskExecutionActivities(service=service).record_workflow_failure(
        snapshot.task_id,
        "simulated activity failure",
    )

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.last_error == "Temporal workflow failed: simulated activity failure"
        events = (
            session.execute(
                select(TaskTimelineEvent).where(TaskTimelineEvent.task_id == snapshot.task_id)
            )
            .scalars()
            .all()
        )
        assert [event.event_type for event in events].count(TimelineEventType.TASK_FAILED) == 1


@pytest.mark.anyio
async def test_global_permission_cap_projects_blocked_node_as_failed(session_factory):
    """The task-level escalation cap must not leave an actionable blocked node."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Bound permission escalation"))
    with session_scope(session_factory) as session:
        plan = ExecutionPlanRepository(session).create(task_id=snapshot.task_id)
        ExecutionPlanRepository(session).add_node(
            plan_id=plan.id,
            node_id="blocked",
            goal="Await permission",
            status=ExecutionPlanNodeStatus.BLOCKED,
        )

    await TaskExecutionActivities(service=service).fail_node_permission_escalation(
        snapshot.task_id, "blocked"
    )

    with session_scope(session_factory) as session:
        node = ExecutionPlanRepository(session).get_node(plan.id, "blocked")
        assert node is not None
        assert node.status == ExecutionPlanNodeStatus.FAILED
        assert node.failure_kind == "permission_escalation_limit"
        assert node.blocker_interaction_id is None


@pytest.mark.anyio
async def test_temporal_delivery_fails_task_after_failed_verification(session_factory):
    """A failed final verifier must override a successful worker result."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Verify final delivery"))
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": snapshot.task_id, "task_text": "Verify final delivery"},
            "result": WorkerResult(
                status="success", summary="Worker reported success."
            ).model_dump(),
            "verification": VerificationReport(
                status="failed",
                summary="Verification failed: required file is missing.",
                failure_kind="test_regression",
                items=[
                    VerificationReportItem(
                        label="deterministic_commands",
                        status="failed",
                        message="qa-hello.txt is missing.",
                    )
                ],
            ).model_dump(),
        }
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    await TaskExecutionActivities(service=service).deliver_result(snapshot.task_id)

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        assert durable_state is None


@pytest.fixture
def session_factory(tmp_path: Path):
    """Create a test session factory with an initialized schema."""
    database_path = tmp_path / "test_temporal_runtime.sqlite"
    engine = create_engine_from_url(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)

    factory = create_session_factory(engine)
    yield factory

    Base.metadata.drop_all(engine)


@pytest.mark.anyio
async def test_temporal_runtime_happy_path(session_factory, tmp_path: Path, monkeypatch):
    """Temporal workflow path should ingest, run in sandbox, and persist result."""
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

    worker = CodexCliWorker(
        runtime_adapter=adapter,
        session_factory=lambda container, **kwargs: _GitMockingSession(container, **kwargs),
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    # 2. Ingest a task
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

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")

    # Start local Temporal test server
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Patch Client.connect to return the test client directly
        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)

        # Create task context
        snapshot, persisted = service.create_task(submission)
        task_id = snapshot.task_id
        assert task_id is not None

        # Start the Temporal worker in background
        activities = TaskExecutionActivities(service=service)
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )

        async with temporal_worker:
            # Submit task: will block until workflow completes when using Temporal
            await service.submit_task(submission, persisted)

    # Verify the database outcome matches expectations
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

        snapshot = session.get(TemporalTaskState, task_id)
        assert snapshot is None


@pytest.mark.anyio
async def test_temporal_runtime_hitl_approval(session_factory, tmp_path: Path, monkeypatch):
    """Workflow should pause at the approval checkpoint, resume on handle_approval, and complete."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

    from orchestrator.state import ApprovalCheckpoint

    # Monkeypatch check_approval to force require manual approval
    monkeypatch.setattr(
        "orchestrator.temporal.activities.check_approval",
        lambda state_input: {"approval": ApprovalCheckpoint(required=True, status="pending")},
    )

    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="final",
                final_output="Done after approval.",
                tool_name=None,
                tool_input=None,
            )
        ]
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        session_factory=lambda container, **kwargs: _GitMockingSession(container, **kwargs),
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    repo_path = tmp_path / "dummy_repo_hitl"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo HITL", encoding="utf-8")
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
            "Init",
        ],
        cwd=repo_path,
    )

    submission = TaskSubmission(
        task_text="Run test with approval",
        repo_url=f"file://{repo_path.resolve()}",
        branch="master",
    )
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")

    async with await WorkflowEnvironment.start_time_skipping() as env:

        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)

        snapshot, persisted = service.create_task(submission)
        task_id = snapshot.task_id

        activities = TaskExecutionActivities(service=service)
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )

        async with temporal_worker:
            # Awaiting a workflow result unlocks automatic time skipping. Keep
            # real time while an operator-style signal is deliberately pending.
            with env.auto_time_skipping_disabled():
                run_task = asyncio.create_task(service.submit_task(submission, persisted))

                for _ in range(20):
                    with session_scope(session_factory) as session:
                        task = session.get(Task, task_id)
                        approval = (task.constraints or {}).get("approval") if task else None
                    if isinstance(approval, dict) and approval.get("status") == "pending":
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail(
                        "Temporal workflow did not persist the pending approval checkpoint."
                    )

                if run_task.done():
                    error = run_task.exception()
                    pytest.fail(
                        "Temporal workflow finished before the approval signal: "
                        f"{error!r}; cause={getattr(error, 'cause', None)!r}"
                    )

                await service.signal_temporal_workflow(task_id, "handle_approval", True)
                await run_task

    # Verify task successfully completed
    with session_scope(session_factory) as session:
        stmt = select(Task).where(Task.id == task_id)
        task = session.execute(stmt).scalar_one_or_none()
        assert task is not None
        assert task.status == "completed"


@pytest.mark.anyio
async def test_temporal_runtime_clarification_interaction_resumes_workflow(
    session_factory, tmp_path: Path, monkeypatch
):
    """A persisted clarification response should resume the Temporal workflow."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

    def require_clarification(state_input):
        task_spec = dict(state_input["task_spec"])
        task_spec.update(
            requires_clarification=True,
            clarification_questions=["Which behavior should change?"],
        )
        return {"task_spec": task_spec}

    monkeypatch.setattr("orchestrator.temporal.activities.check_approval", require_clarification)
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done.")]),
        session_factory=lambda container, **kwargs: _GitMockingSession(container, **kwargs),
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    repo_path = tmp_path / "dummy_repo_clarification"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo Clarification", encoding="utf-8")
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
            "Init",
        ],
        cwd=repo_path,
    )
    submission = TaskSubmission(
        task_text="Clarify this task",
        repo_url=f"file://{repo_path.resolve()}",
        branch="master",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:

        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)
        snapshot, persisted = service.create_task(submission)
        activities = TaskExecutionActivities(service=service)
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )
        async with temporal_worker:
            with env.auto_time_skipping_disabled():
                run_task = asyncio.create_task(service.submit_task(submission, persisted))
                for _ in range(20):
                    cards = service.list_pending_interactions()
                    if cards:
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Temporal workflow did not persist a clarification interaction.")

                clarification = cards[0].interaction
                assert clarification.interaction_type == "clarification"
                service.record_interaction_response(
                    snapshot.task_id,
                    clarification.interaction_id,
                    InteractionResponse(response_data={"answer": "Update README only."}),
                )
                await run_task

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "completed"


@pytest.mark.anyio
async def _exercise_permission_escalation_workflow_with_docker(
    session_factory, tmp_path: Path, monkeypatch
):
    """A worker escalation should persist, signal, reprovision, retry, and finish."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    repo_path = tmp_path / "dummy_repo_permission_escalation"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Permission escalation", encoding="utf-8")
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
            "Init",
        ],
        cwd=repo_path,
    )
    submission = TaskSubmission(
        task_text="Run task requiring permission escalation",
        repo_url=f"file://{repo_path.resolve()}",
        branch="master",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:

        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)
        snapshot, persisted = service.create_task(submission)
        activities = TaskExecutionActivities(service=service)
        attempts = 0

        async def no_op_provisioning(_state_input):
            return {}

        async def request_permission_then_succeed(_state_input):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {
                    "result": WorkerResult(
                        status="failure",
                        summary="Need workspace_write permission.",
                        requested_permission="workspace_write",
                        next_action_hint="request_higher_permission",
                    ).model_dump()
                }
            return {
                "result": WorkerResult(
                    status="success", summary="Completed after permission grant."
                ).model_dump()
            }

        activities.await_result_node = request_permission_then_succeed
        activities.provision_workspace_node = no_op_provisioning
        activities.init_environment_node = no_op_provisioning
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.request_permission_escalation,
                activities.resolve_permission_escalation,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )
        execution_worker = Worker(
            env.client,
            task_queue=CODEX_EXECUTION_TASK_QUEUE,
            activities=[activities.run_worker],
        )
        async with temporal_worker, execution_worker:
            with env.auto_time_skipping_disabled():
                run_task = asyncio.create_task(service.submit_task(submission, persisted))
                for _ in range(300):
                    cards = service.list_pending_interactions()
                    escalation = next(
                        (
                            card.interaction
                            for card in cards
                            if card.interaction.data.get("source") == "worker_permission_escalation"
                        ),
                        None,
                    )
                    if escalation is not None:
                        break
                    await asyncio.sleep(0.1)
                else:
                    if run_task.done():
                        error = run_task.exception()
                        pytest.fail(
                            "Temporal workflow finished before permission escalation: "
                            f"{error!r}; cause={getattr(error, 'cause', None)!r}"
                        )
                    with session_scope(session_factory) as session:
                        temporal_state = session.get(TemporalTaskState, snapshot.task_id)
                    pytest.fail(
                        "Temporal workflow did not persist permission escalation: "
                        f"attempts={attempts}, "
                        f"state={temporal_state.state if temporal_state else None!r}"
                    )

                service.record_interaction_response(
                    snapshot.task_id,
                    escalation.interaction_id,
                    InteractionResponse(response_data={"approved": True}),
                )
                await run_task

    assert attempts == 2
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.constraints["granted_permission"] == "workspace_write"


@pytest.mark.anyio
async def test_permission_escalation_activities_persist_and_apply_grant(session_factory):
    """Escalation activities create one interaction and reset durable state on grant."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Escalate permission"))
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": snapshot.task_id, "task_text": "Escalate permission"},
            "result": WorkerResult(
                status="failure",
                summary="Need workspace_write permission.",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            ).model_dump(),
        }
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    activities = TaskExecutionActivities(service=service)
    await activities.request_permission_escalation(snapshot.task_id)
    cards = service.list_pending_interactions()
    escalation = next(
        card.interaction
        for card in cards
        if card.interaction.data.get("source") == "worker_permission_escalation"
    )
    assert escalation.interaction_type == "permission"
    await activities.request_permission_escalation(snapshot.task_id)
    escalation_cards = [
        card
        for card in service.list_pending_interactions()
        if card.interaction.data.get("source") == "worker_permission_escalation"
    ]
    assert len(escalation_cards) == 1

    await activities.resolve_permission_escalation(snapshot.task_id, True)
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        assert task is not None
        assert durable_state is not None
        resumed = OrchestratorState.model_validate(durable_state.state)
        assert task.constraints["granted_permission"] == "workspace_write"
        assert resumed.result is None
        assert resumed.task.constraints["permission_escalation_retry"] is True


@pytest.mark.anyio
async def test_permission_escalation_rejection_projects_terminal_state(session_factory):
    """Rejected escalation must fail the task and remove resumable workflow state."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Implement bounded change"))
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": snapshot.task_id, "task_text": "Implement bounded change"},
            "result": WorkerResult(
                status="failure",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            ).model_dump(),
        }
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    await TaskExecutionActivities(service=service).resolve_permission_escalation(
        snapshot.task_id, False
    )

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        timeline = TaskTimelineRepository(session).list_by_task(snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.last_error == "Worker permission escalation rejected by operator."
        assert durable_state is None
        assert timeline[-1].event_type == TimelineEventType.APPROVAL_REJECTED


@pytest.mark.anyio
async def test_cancelling_pending_permission_escalation_removes_resumable_state(session_factory):
    """Operator cancellation must win over a pending worker escalation."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Implement bounded change"))
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": snapshot.task_id, "task_text": "Implement bounded change"},
            "result": WorkerResult(
                status="failure",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            ).model_dump(),
        }
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )
    activities = TaskExecutionActivities(service=service)
    await activities.request_permission_escalation(snapshot.task_id)

    cancelled = service.cancel_task(task_id=snapshot.task_id)

    assert cancelled is not None
    assert cancelled.status == "failed"
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        assert durable_state is None


@pytest.mark.anyio
async def test_temporal_run_worker_cancellation_reaches_worker_cleanup(
    session_factory, monkeypatch
):
    """Cancelling the Temporal worker activity must cancel the worker await path."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Cancel worker activity"))
    state = OrchestratorState.model_validate(
        {"task": {"task_id": snapshot.task_id, "task_text": "Cancel worker activity"}}
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def blocking_worker_node(_state_input):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaned_up.set()
            raise

    monkeypatch.setattr("orchestrator.temporal.activities.activity.heartbeat", lambda: None)
    activities = TaskExecutionActivities(service=service)
    activities.await_result_node = blocking_worker_node
    activity_task = asyncio.create_task(activities.run_worker(snapshot.task_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    activity_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activity_task
    assert cleaned_up.is_set()


@pytest.mark.anyio
async def test_temporal_runtime_cancellation_projects_terminal_state(
    session_factory, tmp_path: Path, monkeypatch
):
    """Cancelling a waiting workflow must leave one terminal product projection."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

    from orchestrator.state import ApprovalCheckpoint

    monkeypatch.setattr(
        "orchestrator.temporal.activities.check_approval",
        lambda state_input: {"approval": ApprovalCheckpoint(required=True, status="pending")},
    )
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    repo_path = tmp_path / "dummy_repo_cancel"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo Cancel", encoding="utf-8")
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
            "Init",
        ],
        cwd=repo_path,
    )
    submission = TaskSubmission(
        task_text="Cancel while awaiting approval",
        repo_url=f"file://{repo_path.resolve()}",
        branch="master",
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:

        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)
        snapshot, persisted = service.create_task(submission)
        activities = TaskExecutionActivities(service=service)
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.record_workflow_failure,
            ],
        )

        async with temporal_worker:
            with env.auto_time_skipping_disabled():
                run_task = asyncio.create_task(service.submit_task(submission, persisted))
                for _ in range(20):
                    with session_scope(session_factory) as session:
                        task = session.get(Task, snapshot.task_id)
                        approval = (task.constraints or {}).get("approval") if task else None
                    if isinstance(approval, dict) and approval.get("status") == "pending":
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Temporal workflow did not reach its approval checkpoint.")

                cancelled = service.cancel_task(task_id=snapshot.task_id)
                assert cancelled is not None
                assert cancelled.status == "failed"
                with pytest.raises(WorkflowFailureError):
                    await run_task

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "failed"
        events = (
            session.execute(
                select(TaskTimelineEvent).where(TaskTimelineEvent.task_id == snapshot.task_id)
            )
            .scalars()
            .all()
        )
        assert [event.event_type for event in events].count(TimelineEventType.TASK_CANCELLED) == 1


@pytest.mark.anyio
async def test_temporal_runtime_idempotency_and_retry(session_factory, tmp_path: Path, monkeypatch):
    """Workflow should recover from activity crashes without duplicate events."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="final",
                final_output="Done.",
                tool_name=None,
                tool_input=None,
            )
        ]
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        session_factory=lambda container, **kwargs: _GitMockingSession(container, **kwargs),
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    repo_path = tmp_path / "dummy_repo_idempotency"
    repo_path.mkdir()
    (repo_path / "README.md").write_text("# Dummy Repo Idempotency", encoding="utf-8")
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
            "Init",
        ],
        cwd=repo_path,
    )

    submission = TaskSubmission(
        task_text="Run test with retries",
        repo_url=f"file://{repo_path.resolve()}",
        branch="master",
    )
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")

    async with await WorkflowEnvironment.start_time_skipping() as env:

        async def _mock_connect(*args, **kwargs):
            return env.client

        monkeypatch.setattr(Client, "connect", _mock_connect)

        snapshot, persisted = service.create_task(submission)
        task_id = snapshot.task_id

        activities = TaskExecutionActivities(service=service)

        # Mock load_memory activity to fail on first attempt after DB write
        attempt = 0
        original_load = activities.load_memory

        from temporalio import activity

        @activity.defn(name="load_memory")
        async def mock_load_memory(t_id: str) -> None:
            nonlocal attempt
            attempt += 1
            await original_load(t_id)
            if attempt == 1:
                raise RuntimeError("Transient crash after DB write")

        activities.load_memory = mock_load_memory

        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.decompose_task,
                activities.load_memory,
                activities.provision_workspace,
                activities.run_worker,
                activities.record_workflow_failure,
                activities.verify_result,
                activities.deliver_result,
                activities.persist_memory,
            ],
        )

        async with temporal_worker:
            await service.submit_task(submission, persisted)

    # Verify task successfully completed
    with session_scope(session_factory) as session:
        stmt = select(Task).where(Task.id == task_id)
        task = session.execute(stmt).scalar_one_or_none()
        assert task is not None
        assert task.status == "completed"

        # Verify that memory_loaded timeline event exists exactly once (no duplication)
        from db.enums import TimelineEventType
        from db.models import TaskTimelineEvent

        stmt_events = select(TaskTimelineEvent).where(
            TaskTimelineEvent.task_id == task_id,
            TaskTimelineEvent.event_type == TimelineEventType.MEMORY_LOADED,
        )
        events = session.execute(stmt_events).scalars().all()
        assert len(events) == 1

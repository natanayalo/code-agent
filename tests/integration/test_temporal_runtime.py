from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from db.enums import (
    ExecutionPlanNodeStatus,
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    TimelineEventType,
)
from db.models import (
    Base,
    HumanInteraction,
    Task,
    TaskTimelineEvent,
    TemporalTaskState,
    WorkerRun,
)
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.execution_types import InteractionResponse
from orchestrator.nodes.verification_result import verify_result as evaluate_verification
from orchestrator.state import OrchestratorState, VerificationReport, VerificationReportItem
from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from orchestrator.temporal.queues import CODEX_EXECUTION_TASK_QUEUE
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from orchestrator.verification import resolve_verification_commands
from repositories import (
    ExecutionPlanRepository,
    SessionStateRepository,
    TaskTimelineRepository,
    TemporalCommandRepository,
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


async def _start_workflow_via_dispatcher(
    dispatcher: TemporalCommandDispatcher, client, task_id: str
) -> asyncio.Task[object]:
    """Start a Temporal workflow through the durable command dispatcher."""
    await dispatcher.dispatch_pending()
    handle = client.get_workflow_handle(f"task-{task_id}")
    return asyncio.create_task(handle.result())


async def _wait_for_pending_approval(session_factory: Any, task_id: str) -> None:
    """Wait until classification has projected its durable approval checkpoint."""
    for _ in range(20):
        with session_scope(session_factory) as session:
            task = session.get(Task, task_id)
            approval = (task.constraints or {}).get("approval") if task else None
        if isinstance(approval, dict) and approval.get("status") == "pending":
            return
        await asyncio.sleep(0.1)
    pytest.fail("Temporal workflow did not persist the pending approval checkpoint.")


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


class _CompletionLoopWorker:
    """Script execution and independent-review outcomes for completion-loop tests."""

    def __init__(self, review_outcomes: list[dict] | None = None) -> None:
        self.execution_requests = []
        self.review_requests = []
        self.review_outcomes = list(review_outcomes or [])

    async def run(self, request, *, system_prompt=None) -> WorkerResult:
        if request.task_text.startswith("Perform an independent review"):
            self.review_requests.append(request)
            payload = self.review_outcomes.pop(0)
            return WorkerResult(status="success", summary=json.dumps(payload))
        self.execution_requests.append(request)
        return WorkerResult(
            status="success",
            summary=f"worker pass {len(self.execution_requests)} completed",
            files_changed=["main.py"],
            workspace_id=request.workspace_id or "retained-workspace",
        )


class _VerifierBoundaryWorker:
    """Capture execution, verifier, and reviewer requests through the real activity."""

    def __init__(self) -> None:
        self.verifier_requests = []

    async def run(self, request, *, system_prompt=None) -> WorkerResult:
        if "Independently verify the previously completed task" in request.task_text:
            self.verifier_requests.append(request)
            return WorkerResult(
                status="success",
                summary=json.dumps({"status": "passed", "summary": "read-only verifier passed"}),
            )
        if request.task_text.startswith("Perform an independent review"):
            return WorkerResult(
                status="success",
                summary=json.dumps(
                    {
                        "reviewer_kind": "independent_reviewer",
                        "summary": "no findings",
                        "confidence": 1.0,
                        "outcome": "no_findings",
                        "findings": [],
                    }
                ),
            )
        return WorkerResult(
            status="success",
            summary="initial worker completed",
            files_changed=["main.py"],
            workspace_id=request.workspace_id or "retained-workspace",
        )


class _BlockingRepairWorker(_CompletionLoopWorker):
    """Block one repair execution so cancellation and worker restart can be observed."""

    def __init__(self) -> None:
        super().__init__()
        self.repair_started = asyncio.Event()
        self.repair_cancelled = asyncio.Event()
        self._block_next_repair = True

    async def run(self, request, *, system_prompt=None) -> WorkerResult:
        if request.task_text.startswith("Perform an independent review"):
            return await super().run(request, system_prompt=system_prompt)
        self.execution_requests.append(request)
        if len(self.execution_requests) == 2 and self._block_next_repair:
            self.repair_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self._block_next_repair = False
                self.repair_cancelled.set()
                raise
        return WorkerResult(
            status="success",
            summary=f"worker pass {len(self.execution_requests)} completed",
            files_changed=["main.py"],
            workspace_id=request.workspace_id or "retained-workspace",
        )


def _completion_activity_functions(activities: TaskExecutionActivities) -> list:
    return [
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
    ]


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
async def test_temporal_activity_failure_projects_terminal_task(session_factory, monkeypatch):
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


async def _run_completion_loop_workflow(
    *,
    session_factory,
    service: TaskExecutionService,
    submission: TaskSubmission,
    configure_activities,
) -> tuple[str, dict]:
    """Run one completion-loop workflow against the Temporal test server."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        snapshot, _ = service.create_task(submission)
        activities = TaskExecutionActivities(service=service)
        activities.decompose_task_node = lambda _state: {}
        configure_activities(activities)
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=_completion_activity_functions(activities),
        )
        async with temporal_worker:
            dispatcher = TemporalCommandDispatcher(
                client=env.client,
                session_factory=session_factory,
            )
            run_task = await _start_workflow_via_dispatcher(
                dispatcher,
                env.client,
                snapshot.task_id,
            )
            workflow_result = await run_task
    return snapshot.task_id, workflow_result


@pytest.mark.anyio
async def test_temporal_independent_verifier_request_is_read_only(session_factory):
    """The real verification activity must not grant mutation tools to its inspector."""
    worker = _VerifierBoundaryWorker()
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
        enable_independent_verifier=True,
    )

    _task_id, workflow_result = await _run_completion_loop_workflow(
        session_factory=session_factory,
        service=service,
        submission=TaskSubmission(task_text="Implement main.py"),
        configure_activities=lambda _activities: None,
    )

    assert workflow_result["status"] == "completed"
    assert len(worker.verifier_requests) == 1
    verifier_request = worker.verifier_requests[0]
    assert verifier_request.read_only is True
    assert verifier_request.constraints["read_only"] is True


@pytest.mark.anyio
async def test_temporal_resolves_operator_post_worker_verification_commands(session_factory):
    """Private operator checks must arrive only at the post-worker verifier stage."""
    worker = _CompletionLoopWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    resolved_commands: list[str] = []

    def configure(activities: TaskExecutionActivities) -> None:
        async def verifier(state_input):
            state = OrchestratorState.model_validate(state_input)
            resolved_commands.extend(resolve_verification_commands(state))
            return evaluate_verification(
                state,
                deterministic_verifier_outcome=("passed", "operator checks passed"),
            )

        activities.verify_result_node = verifier
        activities.review_result_node = lambda _state: {}

    _task_id, workflow_result = await _run_completion_loop_workflow(
        session_factory=session_factory,
        service=service,
        submission=TaskSubmission(
            task_text="Implement main.py",
            constraints={
                "verification_commands": ["visible-check"],
                "operator_post_worker_verification_commands": ["private-fixture-command"],
            },
        ),
        configure_activities=configure,
    )

    assert workflow_result["status"] == "completed"
    assert resolved_commands == ["visible-check", "private-fixture-command"]
    assert worker.execution_requests[0].task_spec["verification_commands"] == ["visible-check"]


@pytest.mark.anyio
async def test_temporal_verifier_repair_completes_through_second_worker_pass(session_factory):
    """A verifier repair request must rerun the selected worker and verify again."""
    worker = _CompletionLoopWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    verification_calls = 0
    verification_activity_attempts = 0

    def configure(activities: TaskExecutionActivities) -> None:
        async def verifier(state_input):
            nonlocal verification_calls
            verification_calls += 1
            outcome = (
                ("failed", "main.py still needs repair")
                if verification_calls == 1
                else ("passed", "main.py repair accepted")
            )
            return evaluate_verification(
                OrchestratorState.model_validate(state_input),
                deterministic_verifier_outcome=outcome,
            )

        activities.verify_result_node = verifier
        activities.review_result_node = lambda _state: {}
        original_verify_result = activities.verify_result

        @activity.defn(name="verify_result")
        async def crash_once_after_persist(task_id: str) -> dict:
            nonlocal verification_activity_attempts
            verification_activity_attempts += 1
            decision = await original_verify_result(task_id)
            if verification_activity_attempts == 1:
                raise RuntimeError("simulated crash after verification persistence")
            return decision

        activities.verify_result = crash_once_after_persist

    task_id, workflow_result = await _run_completion_loop_workflow(
        session_factory=session_factory,
        service=service,
        submission=TaskSubmission(task_text="Implement main.py repair"),
        configure_activities=configure,
    )

    assert workflow_result["status"] == "completed"
    assert verification_calls == 2
    assert verification_activity_attempts == 3
    assert len(worker.execution_requests) == 2
    assert "Apply targeted code fixes" in worker.execution_requests[1].task_text
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        events = TaskTimelineRepository(session).list_by_task(task_id)
        assert task is not None and task.status == "completed"
        assert [event.event_type for event in events].count(TimelineEventType.WORKER_COMPLETED) == 2
        assert [event.event_type for event in events].count(
            TimelineEventType.VERIFICATION_COMPLETED
        ) == 2
        assert task.constraints["independent_verifier_repair_passes_used"] == 1
        assert session.get(TemporalTaskState, task_id) is None


@pytest.mark.anyio
async def test_temporal_independent_review_repair_is_reviewed_again(session_factory):
    """Independent-review repair must repeat review instead of skipping acceptance."""
    finding = {
        "title": "High severity bug",
        "category": "logic",
        "confidence": 0.95,
        "file_path": "main.py",
        "severity": "high",
        "why_it_matters": "Behavior can break",
    }
    worker = _CompletionLoopWorker(
        review_outcomes=[
            {
                "reviewer_kind": "independent_reviewer",
                "summary": "repair required",
                "confidence": 0.95,
                "outcome": "findings",
                "findings": [finding],
            },
            {
                "reviewer_kind": "independent_reviewer",
                "summary": "repair accepted",
                "confidence": 0.95,
                "outcome": "no_findings",
                "findings": [],
            },
        ]
    )
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    def configure(activities: TaskExecutionActivities) -> None:
        activities.verify_result_node = lambda state_input: evaluate_verification(
            OrchestratorState.model_validate(state_input),
            deterministic_verifier_outcome=("passed", "deterministic checks passed"),
        )

    task_id, workflow_result = await _run_completion_loop_workflow(
        session_factory=session_factory,
        service=service,
        submission=TaskSubmission(
            task_text="Implement reviewed change",
            constraints={"independent_review_enable_repair_handoff": True},
        ),
        configure_activities=configure,
    )

    assert workflow_result["status"] == "completed"
    assert len(worker.execution_requests) == 2
    assert len(worker.review_requests) == 2
    assert "independent review findings" in worker.execution_requests[1].task_text
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        assert task is not None and task.status == "completed"
        assert task.constraints["independent_review_repair_passes_used"] == 1
        assert session.get(TemporalTaskState, task_id) is None


@pytest.mark.anyio
async def test_temporal_repair_exhaustion_projects_manual_follow_up(session_factory):
    """A failed repair pass must terminate with one actionable failed projection."""
    worker = _CompletionLoopWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)

    def configure(activities: TaskExecutionActivities) -> None:
        activities.verify_result_node = lambda state_input: evaluate_verification(
            OrchestratorState.model_validate(state_input),
            deterministic_verifier_outcome=("failed", "main.py remains invalid"),
        )
        activities.review_result_node = lambda _state: {}

    task_id, workflow_result = await _run_completion_loop_workflow(
        session_factory=session_factory,
        service=service,
        submission=TaskSubmission(
            task_text="Implement main.py repair",
            constraints={"independent_verifier_max_repair_passes": 1},
        ),
        configure_activities=configure,
    )

    assert workflow_result["status"] == "failed"
    assert "manual follow-up is required" in workflow_result["summary"]
    assert len(worker.execution_requests) == 2
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        events = TaskTimelineRepository(session).list_by_task(task_id)
        assert task is not None and task.status == "failed"
        assert [event.event_type for event in events].count(TimelineEventType.TASK_FAILED) == 1
        assert session.get(TemporalTaskState, task_id) is None


@pytest.mark.anyio
async def test_temporal_repair_recovers_after_worker_restart(session_factory):
    """A repair Activity lost with its worker must retry without duplicate state."""
    worker = _CompletionLoopWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    verification_calls = 0
    restart_failures = 0

    async with await WorkflowEnvironment.start_time_skipping() as env:
        snapshot, _ = service.create_task(TaskSubmission(task_text="Implement restart repair"))
        activities = TaskExecutionActivities(service=service)
        activities.decompose_task_node = lambda _state: {}

        async def verifier(state_input):
            nonlocal verification_calls
            verification_calls += 1
            outcome = (
                ("failed", "repair required")
                if verification_calls == 1
                else ("passed", "repair accepted")
            )
            return evaluate_verification(
                OrchestratorState.model_validate(state_input),
                deterministic_verifier_outcome=outcome,
            )

        activities.verify_result_node = verifier
        activities.review_result_node = lambda _state: {}
        original_run_worker = activities.run_worker

        @activity.defn(name="run_worker")
        async def restart_once_during_repair(task_id: str) -> dict:
            nonlocal restart_failures
            state = await service._run_blocking(activities._get_current_state, task_id)
            if state.completion_loop.phase == "repair_requested" and restart_failures == 0:
                restart_failures += 1
                raise RuntimeError("simulated worker restart during repair")
            return await original_run_worker(task_id)

        activity_functions = [
            restart_once_during_repair if fn == activities.run_worker else fn
            for fn in _completion_activity_functions(activities)
        ]
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activity_functions,
        )
        async with temporal_worker:
            dispatcher = TemporalCommandDispatcher(
                client=env.client,
                session_factory=session_factory,
            )
            run_task = await _start_workflow_via_dispatcher(
                dispatcher,
                env.client,
                snapshot.task_id,
            )
            workflow_result = await run_task

    assert workflow_result["status"] == "completed"
    assert restart_failures == 1
    assert len(worker.execution_requests) == 2
    assert verification_calls == 2
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        events = TaskTimelineRepository(session).list_by_task(snapshot.task_id)
        assert task is not None and task.status == "completed"
        assert [event.event_type for event in events].count(TimelineEventType.WORKER_COMPLETED) == 2


@pytest.mark.anyio
async def test_temporal_cancellation_during_repair_projects_one_terminal_state(session_factory):
    """Cancelling a blocked repair pass must stop the worker and persist one cancellation."""
    worker = _BlockingRepairWorker()
    service = TaskExecutionService(session_factory=session_factory, worker=worker)
    verification_calls = 0

    async with await WorkflowEnvironment.start_time_skipping() as env:
        snapshot, _ = service.create_task(TaskSubmission(task_text="Cancel blocked repair"))
        activities = TaskExecutionActivities(service=service)
        activities.decompose_task_node = lambda _state: {}

        async def verifier(state_input):
            nonlocal verification_calls
            verification_calls += 1
            return evaluate_verification(
                OrchestratorState.model_validate(state_input),
                deterministic_verifier_outcome=("failed", "repair required"),
            )

        activities.verify_result_node = verifier
        activities.review_result_node = lambda _state: {}
        temporal_worker = Worker(
            env.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=_completion_activity_functions(activities),
        )
        async with temporal_worker:
            dispatcher = TemporalCommandDispatcher(
                client=env.client,
                session_factory=session_factory,
            )
            with env.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(
                    dispatcher,
                    env.client,
                    snapshot.task_id,
                )
                await asyncio.wait_for(worker.repair_started.wait(), timeout=5)
                cancelled = service.cancel_task(task_id=snapshot.task_id)
                assert cancelled is not None and cancelled.status == "cancelled"
                await dispatcher.dispatch_pending()
                with pytest.raises(WorkflowFailureError):
                    await run_task

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        events = TaskTimelineRepository(session).list_by_task(snapshot.task_id)
        assert task is not None and task.status == "cancelled"
        assert [event.event_type for event in events].count(TimelineEventType.TASK_CANCELLED) == 1
        assert session.get(TemporalTaskState, snapshot.task_id) is None


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

    # Start local Temporal test server
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create task context
        snapshot, _ = service.create_task(submission)
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client, session_factory=session_factory
            )
            run_task = await _start_workflow_via_dispatcher(dispatcher, env.client, task_id)
            await run_task

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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        snapshot, _ = service.create_task(submission)
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client,
                session_factory=session_factory,
            )
            # Awaiting a workflow result unlocks automatic time skipping. Keep
            # real time while an operator-style signal is deliberately pending.
            with env.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(dispatcher, env.client, task_id)

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

                with session_scope(session_factory) as session:
                    TemporalCommandRepository(session).enqueue(
                        task_id=task_id,
                        command_type="signal",
                        command_key=f"task:{task_id}:approval:test",
                        payload={"signal_name": "handle_approval", "signal_arg": True},
                    )
                await dispatcher.dispatch_pending()
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
    """A pre-classification clarification response must not skip routing."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)
    if not _docker_available():
        pytest.skip("Docker daemon is unavailable")

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
        snapshot, _ = service.create_task(submission)
        with session_scope(session_factory) as session:
            clarification = HumanInteraction(
                task_id=snapshot.task_id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.PENDING,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="Which behavior should change?",
                data={"source": "test", "resume_token": f"clarification-{snapshot.task_id}"},
            )
            session.add(clarification)
            session.flush()
            clarification_id = clarification.id
        service.record_interaction_response(
            snapshot.task_id,
            clarification_id,
            InteractionResponse(response_data={"answer": "Update README only."}),
        )
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client, session_factory=session_factory
            )
            with env.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(
                    dispatcher, env.client, snapshot.task_id
                )
                await dispatcher.dispatch_pending()
                await run_task

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "completed"
        assert task.chosen_worker is not None
        assert task.task_spec is not None
        timeline = TaskTimelineRepository(session).list_by_task(snapshot.task_id)
        assert any(event.event_type is TimelineEventType.INTERACTION_RESOLVED for event in timeline)
        assert any(
            event.event_type is TimelineEventType.TASK_SPEC_AND_ROUTE_GENERATED
            for event in timeline
        )


@pytest.mark.anyio
async def _exercise_permission_escalation_workflow_with_docker(
    session_factory, tmp_path: Path, monkeypatch
):
    """A worker escalation should persist, signal, reprovision, retry, and finish."""
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: tmp_path)

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
        snapshot, _ = service.create_task(submission)
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client, session_factory=session_factory
            )
            with env.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(
                    dispatcher, env.client, snapshot.task_id
                )
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
async def test_permission_escalation_activities_persist_and_apply_grant(
    session_factory, monkeypatch
):
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
                failure_kind="permission_denied",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            ).model_dump(),
        }
    )
    with session_scope(session_factory) as session:
        SessionStateRepository(session).upsert(
            session_id=snapshot.session_id,
            active_goal="Prior successful task",
            identified_risks={"worker_status": "success"},
        )
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    await TaskExecutionActivities(service=service).resolve_permission_escalation(
        snapshot.task_id, False
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
        assert [event.event_type for event in timeline].count(
            TimelineEventType.APPROVAL_REJECTED
        ) == 1
        context = SessionStateRepository(session).get(snapshot.session_id)
        assert context is not None
        assert context.decisions_made["approval_status"] == "rejected"
        assert context.identified_risks["worker_status"] == "failure"
        assert context.identified_risks["worker_failure_kind"] == "permission_denied"
        assert context.identified_risks["requested_permission"] == "workspace_write"


@pytest.mark.anyio
async def test_temporal_approval_rejection_persists_compact_session_context(
    session_factory,
    monkeypatch,
):
    """An initial approval rejection must replace stale session continuity state."""
    from orchestrator.state import ApprovalCheckpoint

    monkeypatch.setattr(
        "orchestrator.temporal.activities.check_approval",
        lambda _state: {
            "approval": ApprovalCheckpoint(
                required=True,
                status="pending",
                approval_type="manual_approval",
            )
        },
    )
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Reject this approval"))
    with session_scope(session_factory) as session:
        SessionStateRepository(session).upsert(
            session_id=snapshot.session_id,
            active_goal="Prior successful task",
            identified_risks={
                "worker_status": "success",
                "worker_failure_kind": None,
                "requested_permission": "workspace_write",
            },
        )

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        activities = TaskExecutionActivities(service=service)
        temporal_worker = Worker(
            environment.client,
            task_queue="task-execution-queue",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=[
                activities.classify_and_plan,
                activities.persist_rejected_session_state,
                activities.record_workflow_failure,
            ],
        )
        async with temporal_worker:
            dispatcher = TemporalCommandDispatcher(
                client=environment.client,
                session_factory=session_factory,
            )
            with environment.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(
                    dispatcher,
                    environment.client,
                    snapshot.task_id,
                )
                await _wait_for_pending_approval(session_factory, snapshot.task_id)

                rejected = service.apply_task_approval_decision(
                    task_id=snapshot.task_id,
                    approved=False,
                )
                assert rejected.status == "applied"
                await dispatcher.dispatch_pending()
                workflow_result = await run_task

    assert workflow_result == {"status": "rejected", "summary": "Manual approval rejected."}
    with session_scope(session_factory) as session:
        context = SessionStateRepository(session).get(snapshot.session_id)
        assert context is not None
        assert context.active_goal == "Reject this approval"
        assert context.decisions_made["approval_status"] == "rejected"
        assert context.identified_risks["worker_status"] is None
        assert context.identified_risks["worker_failure_kind"] is None
        assert context.identified_risks["requested_permission"] is None


@pytest.mark.anyio
async def test_repair_permission_rejection_persists_manual_handoff_for_delivery(
    session_factory,
):
    """Rejected repair permission must retain an idempotent deliverable snapshot."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Repair bounded change"))
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": snapshot.task_id,
                "task_text": "Repair bounded change",
                "constraints": {"independent_verifier_repair_request": "fix tests"},
            },
            "result": WorkerResult(
                status="failure",
                summary="Need workspace write permission.",
                requested_permission="workspace_write",
                next_action_hint="request_higher_permission",
            ).model_dump(),
            "completion_loop": {
                "phase": "repair_requested",
                "repair_pass": 1,
                "repair_source": "verifier",
            },
        }
    )
    with session_scope(session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=snapshot.task_id, state=state.model_dump(mode="json")
        )

    activities = TaskExecutionActivities(service=service)
    await activities.resolve_permission_escalation(snapshot.task_id, False)
    await activities.resolve_permission_escalation(snapshot.task_id, False)

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        timeline = TaskTimelineRepository(session).list_by_task(snapshot.task_id)
        assert task is not None and task.status == "in_progress"
        assert durable_state is not None
        handoff = OrchestratorState.model_validate(durable_state.state)
        assert handoff.completion_loop.phase == "manual_follow_up"
        assert handoff.result is not None
        assert handoff.result.failure_kind == "incomplete_delivery"
        assert handoff.result.next_action_hint == "await_manual_follow_up"
        assert handoff.result.summary.count("Repair permission was rejected") == 1
        assert [event.event_type for event in timeline].count(
            TimelineEventType.APPROVAL_REJECTED
        ) == 1


@pytest.mark.anyio
async def test_permission_escalation_missing_snapshot_for_active_task_still_fails(
    session_factory,
):
    """Missing resumable state is only idempotent after the task is terminal."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Implement bounded change"))

    with pytest.raises(RuntimeError, match="unavailable for permission escalation"):
        await TaskExecutionActivities(service=service).resolve_permission_escalation(
            snapshot.task_id,
            False,
        )


@pytest.mark.anyio
async def test_permission_escalation_missing_task_still_fails(session_factory):
    """A retry cannot be treated as complete when the task itself is absent."""
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=CodexCliWorker(runtime_adapter=_ScriptedAdapter([])),
    )

    with pytest.raises(RuntimeError, match="unavailable for permission escalation"):
        await TaskExecutionActivities(service=service).resolve_permission_escalation(
            "missing-task",
            False,
        )


@pytest.mark.anyio
async def test_cancelling_pending_permission_escalation_removes_resumable_state(
    session_factory, monkeypatch
):
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
    assert cancelled.status == "cancelled"
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        durable_state = TemporalTaskStateRepository(session).get(task_id=snapshot.task_id)
        assert task is not None
        assert task.status == "cancelled"
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
        {
            "task": {"task_id": snapshot.task_id, "task_text": "Cancel worker activity"},
            "dispatch": {
                "workspace_id": f"workspace-{snapshot.task_id}",
                "runtime_manifest": {
                    "sandbox": {"workspace_root": "/tmp/code-agent-workspaces"},
                    "worker": {"workspace_id": f"workspace-{snapshot.task_id}"},
                },
            },
        }
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
            return {
                "result": WorkerResult(
                    status="failure",
                    summary="Worker stopped after operator cancellation.",
                    failure_kind="timeout",
                ).model_dump(mode="json")
            }

    monkeypatch.setattr("orchestrator.temporal.activities.activity.heartbeat", lambda: None)
    activities = TaskExecutionActivities(service=service)
    activities.await_result_node = blocking_worker_node
    activity_task = asyncio.create_task(activities.run_worker(snapshot.task_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    activity_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activity_task
    assert cleaned_up.is_set()
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        worker_run = session.query(WorkerRun).filter_by(task_id=snapshot.task_id).one()
        assert task is not None and task.status == "cancelled"
        assert worker_run.status == "failure"
        assert worker_run.artifact_index[0]["name"] == "workspace"


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
        snapshot, _ = service.create_task(submission)
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client, session_factory=session_factory
            )
            with env.auto_time_skipping_disabled():
                run_task = await _start_workflow_via_dispatcher(
                    dispatcher, env.client, snapshot.task_id
                )
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
                assert cancelled.status == "cancelled"
                await dispatcher.dispatch_pending()
                with pytest.raises(WorkflowFailureError):
                    await run_task

    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        assert task is not None
        assert task.status == "cancelled"
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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        snapshot, _ = service.create_task(submission)
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
            dispatcher = TemporalCommandDispatcher(
                client=env.client, session_factory=session_factory
            )
            run_task = await _start_workflow_via_dispatcher(dispatcher, env.client, task_id)
            await run_task

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

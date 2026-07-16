"""Integration tests for the ExecutionPlanRepository."""

from datetime import datetime

import repositories.sqlalchemy_plan as sqlalchemy_plan_module
from db.base import utc_now
from db.enums import ExecutionPlanNodeStatus
from repositories.session import session_scope
from repositories.sqlalchemy_plan import ExecutionPlanRepository
from repositories.sqlalchemy_session import SessionRepository, UserRepository
from repositories.sqlalchemy_task import TaskRepository


def test_create_and_get_plan(session_factory):
    """Test creating and retrieving an execution plan."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        user = user_repo.create(external_user_id="test-plan-user-1", display_name="Test User")

        session_repo = SessionRepository(session)
        sess = session_repo.create(
            user_id=user.id, channel="web", external_thread_id="test-thread-1"
        )

        task_repo = TaskRepository(session)
        task = task_repo.create(session_id=sess.id, task_text="Test Task")

        repo = ExecutionPlanRepository(session)
        plan = repo.create(task_id=task.id)

        assert plan.id is not None
        assert plan.task_id == task.id

        fetched_plan = repo.get_by_task_id(task.id)
        assert fetched_plan is not None
        assert fetched_plan.id == plan.id

        fetched_plan_by_id = repo.get_by_id(plan.id)
        assert fetched_plan_by_id is not None
        assert fetched_plan_by_id.id == plan.id


def test_add_and_update_node(session_factory):
    """Test adding and updating a node in an execution plan."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        user = user_repo.create(external_user_id="test-plan-user-2", display_name="Test User")

        session_repo = SessionRepository(session)
        sess = session_repo.create(
            user_id=user.id, channel="web", external_thread_id="test-thread-2"
        )

        task_repo = TaskRepository(session)
        task = task_repo.create(session_id=sess.id, task_text="Test Task")

        repo = ExecutionPlanRepository(session)
        plan = repo.create(task_id=task.id)

        node = repo.add_node(
            plan_id=plan.id,
            node_id="node-1",
            goal="Test goal",
            depends_on=["node-0"],
            assigned_worker_profile="coder",
            task_spec={"goal": "Test goal", "task_type": "feature"},
            node_kind="implement",
            aggregation_role="mutation",
            execution_mode="read_only",
            parallel_safe=True,
        )
        assert node.plan_id == plan.id
        assert node.node_id == "node-1"
        assert node.goal == "Test goal"
        assert node.status == ExecutionPlanNodeStatus.PENDING
        assert node.depends_on == ["node-0"]
        assert node.assigned_worker_profile == "coder"
        assert node.task_spec == {"goal": "Test goal", "task_type": "feature"}
        assert node.node_kind == "implement"
        assert node.aggregation_role == "mutation"
        assert node.execution_mode == "read_only"
        assert node.parallel_safe is True

        now = utc_now()
        updated_node = repo.update_node(
            plan_id=plan.id,
            node_id="node-1",
            status=ExecutionPlanNodeStatus.ACTIVE,
            started_at=now,
            retry_count=1,
            result_summary="Completed",
            changed_files=["src/example.py"],
            output_artifacts=[{"name": "report"}],
            failure_kind=None,
            worker_run_id=None,
            execution_mode="mutable",
            parallel_safe=False,
        )
        assert updated_node is not None
        assert updated_node.status == ExecutionPlanNodeStatus.ACTIVE
        assert updated_node.started_at == now
        assert updated_node.retry_count == 1
        assert updated_node.result_summary == "Completed"
        assert updated_node.changed_files == ["src/example.py"]
        assert updated_node.execution_mode == "mutable"
        assert updated_node.parallel_safe is False

        fetched_node = repo.get_node(plan.id, "node-1")
        assert fetched_node is not None
        assert fetched_node.status == ExecutionPlanNodeStatus.ACTIVE
        assert fetched_node.started_at == now


def test_start_attempt_allocates_monotonic_numbers(session_factory):
    """Retry/resume attempt evidence must not reuse a node attempt number."""
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="test-attempt-user", display_name="Test"
        )
        conversation = SessionRepository(session).create(
            user_id=user.id, channel="web", external_thread_id="test-attempt-thread"
        )
        task = TaskRepository(session).create(session_id=conversation.id, task_text="Test task")
        repo = ExecutionPlanRepository(session)
        plan = repo.create(task_id=task.id)
        repo.add_node(plan_id=plan.id, node_id="node", goal="Test node")

        evidence = {"node_id": "node"}
        first = repo.start_attempt(
            plan_id=plan.id,
            node_id="node",
            effective_input_summary=evidence,
            effective_input_digest="a" * 64,
            worker_type=None,
            worker_profile=None,
            runtime_mode=None,
            workspace_id=None,
            task_trace_id=None,
            worker_run_id="worker-run-1",
        )
        second = repo.start_attempt(
            plan_id=plan.id,
            node_id="node",
            effective_input_summary=evidence,
            effective_input_digest="b" * 64,
            worker_type=None,
            worker_profile=None,
            runtime_mode=None,
            workspace_id=None,
            task_trace_id=None,
        )

        assert [first.attempt_number, second.attempt_number] == [1, 2]
        assert first.worker_run_id == "worker-run-1"


def test_finish_attempt_normalizes_finished_at_before_persistence(session_factory, monkeypatch):
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="finish-attempt-user")
        conversation = SessionRepository(session).create(
            user_id=user.id, channel="web", external_thread_id="finish-attempt-thread"
        )
        task = TaskRepository(session).create(session_id=conversation.id, task_text="Test task")
        repo = ExecutionPlanRepository(session)
        plan = repo.create(task_id=task.id)
        repo.add_node(plan_id=plan.id, node_id="node", goal="Test node")
        attempt = repo.start_attempt(
            plan_id=plan.id,
            node_id="node",
            effective_input_summary={},
            effective_input_digest="a" * 64,
            worker_type=None,
            worker_profile=None,
            runtime_mode=None,
            workspace_id=None,
            task_trace_id=None,
        )
        monkeypatch.setattr(sqlalchemy_plan_module, "utc_now", lambda: datetime(2026, 1, 1))

        completed = repo.finish_attempt(
            attempt_id=attempt.id, status="completed", failure_kind=None
        )

        assert completed is not None
        assert completed.finished_at is not None
        assert completed.finished_at.tzinfo is not None

"""Integration tests for the ExecutionPlanRepository."""

from datetime import UTC, datetime

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
        )
        assert node.plan_id == plan.id
        assert node.node_id == "node-1"
        assert node.goal == "Test goal"
        assert node.status == ExecutionPlanNodeStatus.PENDING
        assert node.depends_on == ["node-0"]
        assert node.assigned_worker_profile == "coder"

        now = datetime.now(UTC)
        updated_node = repo.update_node(
            plan_id=plan.id,
            node_id="node-1",
            status=ExecutionPlanNodeStatus.ACTIVE,
            started_at=now,
            retry_count=1,
        )
        assert updated_node is not None
        assert updated_node.status == ExecutionPlanNodeStatus.ACTIVE
        assert updated_node.started_at == now
        assert updated_node.retry_count == 1

        fetched_node = repo.get_node(plan.id, "node-1")
        assert fetched_node is not None
        assert fetched_node.status == ExecutionPlanNodeStatus.ACTIVE
        assert fetched_node.started_at == now

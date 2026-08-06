"""Unit tests for low-coverage SQLAlchemy repositories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.enums import (
    ExecutionPlanNodeStatus,
    HumanInteractionStatus,
    MemoryProposalCategory,
    MemoryProposalStatus,
    ProposalStatus,
    ProposalType,
    TaskStatus,
    WorkerNodeStatus,
    WorkerType,
)
from db.models import Session as ConversationSession
from db.models import User
from repositories import (
    ArtifactRepository,
    ExecutionPlanRepository,
    HumanInteractionRepository,
    InboundDeliveryRepository,
    MemoryProposalRepository,
    ObservationRepository,
    ProposalRepository,
    SessionRepository,
    TaskRepository,
    TemporalCommandRepository,
    UserRepository,
    WorkerNodeRepository,
    WorkerRunRepository,
)


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(bind=db_engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def sample_user_and_session(session):
    user = User(external_user_id="u1", display_name="User 1")
    session.add(user)
    session.flush()
    conv = ConversationSession(id="s1", user_id=user.id, channel="test", external_thread_id="t1")
    session.add(conv)
    session.flush()
    return user, conv


def test_task_repository_creation_and_status(session, sample_user_and_session):
    repo = TaskRepository(session)
    user, conv = sample_user_and_session

    # 1. Create task
    task = repo.create(
        session_id=conv.id,
        task_text="Build feature",
        repo_url="http://repo",
        branch="main",
        callback_url="http://callback",
        worker_override="codex",
        constraints={"c": 1},
        task_spec={"goal": "build"},
        budget={"b": 2},
        secrets={"s": "val"},
        secrets_encrypted=True,
        status="pending",
        priority=10,
        queue_lane="primary",
        max_attempts=5,
        chosen_worker="codex",
        chosen_profile="profile-1",
        runtime_mode="native_agent",
        orchestration_runtime="temporal",
        route_reason="reason-1",
        trace_context={"trace_id": "123"},
        repair_for_task_id="prev-task-1",
    )
    assert task.id is not None
    assert task.task_text == "Build feature"

    # 2. Get & Set task_spec
    assert repo.get(task.id) == task
    assert repo.get("nonexistent") is None

    assert repo.set_task_spec(task_id="nonexistent", task_spec={"g": 2}) is None
    updated_task = repo.set_task_spec(task_id=task.id, task_spec={"goal": "updated"})
    assert updated_task.task_spec == {"goal": "updated"}

    # 3. List by session & list_all
    assert repo.list_by_session(conv.id) == [task]
    assert repo.list_all(session_id=conv.id, status="pending", preload_history=True) == [task]
    assert repo.list_all(session_id=conv.id, status=TaskStatus.PENDING, preload_history=False) == [
        task
    ]

    # 4. is_execution_busy
    assert repo.is_execution_busy() is True


def test_task_repository_routing_and_metrics(session, sample_user_and_session):
    repo = TaskRepository(session)
    user, conv = sample_user_and_session

    task = repo.create(session_id=conv.id, task_text="Routing task", status="pending")

    # 5. set_route & update_status
    assert repo.set_route(task_id="nonexistent", chosen_worker="codex", route_reason="r") is None
    routed_task = repo.set_route(
        task_id=task.id,
        chosen_worker="antigravity",
        chosen_profile="p2",
        runtime_mode="tool_loop",
        route_reason="r2",
    )
    assert routed_task.chosen_worker == WorkerType.ANTIGRAVITY

    assert repo.update_status(task_id="nonexistent", status="completed") is None
    updated_st = repo.update_status(task_id=task.id, status=TaskStatus.IN_PROGRESS)
    assert updated_st.status == TaskStatus.IN_PROGRESS

    # 6. cancel & terminal check
    assert repo.cancel(task_id="nonexistent") == (None, False)
    cancelled_t, success = repo.cancel(task_id=task.id)
    assert success is True
    assert cancelled_t.status == TaskStatus.CANCELLED
    # Second cancel is idempotent
    cancelled_t2, success2 = repo.cancel(task_id=task.id)
    assert success2 is False

    # 7. Metrics & runtime drain metrics
    now = datetime.now(UTC)
    metrics = repo.get_metrics(since=now - timedelta(hours=1))
    assert metrics["total_tasks"] == 1
    assert "status_counts" in metrics

    drain = repo.get_runtime_drain_metrics(cutover_at=now - timedelta(days=1))
    assert "active_legacy_task_count" in drain


def test_proposal_repository_full_coverage(session, sample_user_and_session):
    repo = ProposalRepository(session)
    user, conv = sample_user_and_session

    # 1. Create proposal
    prop = repo.create_proposal(
        session_id=conv.id,
        title="Refactor API",
        summary="Improve error handling",
        content="Details...",
        status=ProposalStatus.PENDING_REVIEW,
        proposal_type=ProposalType.SCOUT,
        metadata_payload={"meta": "val"},
    )
    assert prop.id is not None

    # 2. Get proposal & update status
    assert repo.get_proposal(prop.id) == prop
    with pytest.raises(ValueError, match="not found"):
        repo.get_proposal("nonexistent")

    updated_prop = repo.update_proposal_status(prop.id, ProposalStatus.ACCEPTED)
    assert updated_prop.status == ProposalStatus.ACCEPTED

    # 3. List proposals
    listed = repo.list_proposals(
        status=ProposalStatus.ACCEPTED,
        proposal_type=ProposalType.SCOUT,
        session_id=conv.id,
        limit=10,
    )
    assert listed == [prop]


def test_worker_node_repository_full_coverage(session):
    repo = WorkerNodeRepository(session)
    now = datetime.now(UTC)

    # 1. Register worker (new active node)
    node1 = repo.register_worker(
        worker_id="w1",
        worker_type="codex",
        now=now,
        capacity=2,
        process_identity="proc-1",
        supported_profiles=["p1"],
        capabilities={"worker_types": ["codex"]},
    )
    assert node1.worker_id == "w1"
    assert repo.get_by_worker_id("w1") == node1
    assert repo.get_by_worker_id("nonexistent") is None

    # 2. Ensure worker
    node1_again = repo.ensure_worker(worker_id="w1", worker_type="codex", now=now)
    assert node1_again == node1
    node2 = repo.ensure_worker(worker_id="w2", worker_type="antigravity", now=now)
    assert node2.worker_id == "w2"

    # 3. Heartbeat
    assert repo.heartbeat(worker_id="nonexistent", now=now) is None
    assert repo.heartbeat(worker_id="w1", now=now) == WorkerNodeStatus.ACTIVE

    # 4. Record failure & quarantine threshold
    assert repo.record_failure(worker_id="nonexistent", failure_kind="provider_auth") is None
    # Ignored failure kind
    node1_ign = repo.record_failure(worker_id="w1", failure_kind="test_regression")
    assert node1_ign.consecutive_failures == 0

    # Quarantining after threshold
    repo.record_failure(worker_id="w1", failure_kind="provider_auth", threshold=2)
    node1_q = repo.record_failure(worker_id="w1", failure_kind="provider_auth", threshold=2)
    assert node1_q.status == WorkerNodeStatus.QUARANTINED

    # Re-registering preserves quarantine
    node1_refreshed = repo.register_worker(worker_id="w1", worker_type="codex", now=now)
    assert node1_refreshed.status == WorkerNodeStatus.QUARANTINED

    # Record success resets failures
    node1_succ = repo.record_success(worker_id="w1")
    assert node1_succ.consecutive_failures == 0
    assert repo.record_success(worker_id="nonexistent") is None

    # 5. Sweep stale workers
    old_time = now - timedelta(minutes=10)
    node2.last_heartbeat_at = old_time
    session.flush()
    swept = repo.sweep_stale_workers(now=now, threshold_seconds=60)
    assert swept == 1
    assert node2.status == WorkerNodeStatus.OFFLINE

    # Offline heartbeat restores active
    assert repo.heartbeat(worker_id="w2", now=now) == WorkerNodeStatus.ACTIVE

    # 6. Supported worker types helper
    types = repo.supported_worker_types(node1)
    assert WorkerType.CODEX in types


def test_temporal_command_repository_full_coverage(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="temporal task")

    repo = TemporalCommandRepository(session)

    # 1. Enqueue
    repo.enqueue(
        task_id=task.id,
        command_type="start",
        command_key="ck-1",
        payload={"workflow": "w1"},
    )
    # Idempotent enqueue with same command_key
    repo.enqueue(
        task_id=task.id,
        command_type="start",
        command_key="ck-1",
        payload={"workflow": "w1"},
    )

    # 2. Claim pending
    claimed = repo.claim_pending(limit=10, lease_seconds=30)
    assert len(claimed) == 1
    cmd = claimed[0]
    token = cmd.claim_token
    assert token is not None

    # 3. Mark delivered
    assert repo.mark_delivered(command_id=cmd.id, claim_token=token) is True
    assert repo.mark_delivered(command_id=cmd.id, claim_token="wrong") is False

    # 4. Enqueue second command, claim and mark failed (with retry and dead-letter)
    repo.enqueue(
        task_id=task.id,
        command_type="signal",
        command_key="ck-2",
        payload={},
    )
    claimed2 = repo.claim_pending(limit=10, lease_seconds=30)
    assert len(claimed2) == 1
    cmd2 = claimed2[0]

    # Retry
    now = datetime.now(UTC)
    assert (
        repo.mark_failed(
            command_id=cmd2.id,
            claim_token=cmd2.claim_token,
            error=RuntimeError("err"),
            retry_at=now,
        )
        is True
    )

    # Claim again and dead-letter (retry_at=None)
    claimed2_retry = repo.claim_pending(limit=10, lease_seconds=30)
    assert len(claimed2_retry) == 1
    assert (
        repo.mark_failed(
            command_id=cmd2.id,
            claim_token=claimed2_retry[0].claim_token,
            error=RuntimeError("dead"),
            retry_at=None,
        )
        is True
    )

    # 5. Supersede for cancel
    task2 = task_repo.create(session_id=conv.id, task_text="task2 cancel")
    repo.enqueue(
        task_id=task2.id,
        command_type="start",
        command_key="ck-3",
        payload={},
    )
    assert repo.supersede_for_cancel(task_id=task2.id) is True


def test_execution_plan_repository_full_coverage(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="parent task")

    plan_repo = ExecutionPlanRepository(session)
    plan = plan_repo.create(task_id=task.id)
    assert plan.id is not None
    assert plan_repo.get_by_id(plan.id) == plan
    assert plan_repo.get_by_task_id(task.id) == plan

    # Create node
    node = plan_repo.add_node(
        plan_id=plan.id,
        node_id="n1",
        goal="Inspect codebase",
        node_kind="inspect",
        depends_on=["parent"],
        task_spec={"goal": "inspect"},
        status=ExecutionPlanNodeStatus.PENDING,
    )
    assert node.id is not None


def test_worker_run_and_artifact_repositories(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="run task")

    run_repo = WorkerRunRepository(session)
    now = datetime.now(UTC)
    run = run_repo.create(
        task_id=task.id,
        worker_type="codex",
        workspace_id="ws-1",
        status="running",
        started_at=now,
    )
    assert run.id is not None

    art_repo = ArtifactRepository(session)
    art = art_repo.create(
        run_id=run.id,
        artifact_type="diff",
        name="patch.diff",
        uri="file:///patch.diff",
    )
    assert art.id is not None
    assert len(art_repo.list_by_run(run.id)) == 1
    assert art_repo.delete_by_run(run.id) == 1


def test_execution_plan_nodes_and_attempts(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="parent task")

    plan_repo = ExecutionPlanRepository(session)
    plan = plan_repo.create(task_id=task.id)
    _node = plan_repo.add_node(
        plan_id=plan.id,
        node_id="n1",
        goal="Inspect codebase",
        node_kind="inspect",
        depends_on=["parent"],
        task_spec={"goal": "inspect"},
        status=ExecutionPlanNodeStatus.PENDING,
    )

    # update_node
    assert plan_repo.update_node(plan_id=plan.id, node_id="nonexistent") is None
    updated_node = plan_repo.update_node(
        plan_id=plan.id,
        node_id="n1",
        status=ExecutionPlanNodeStatus.COMPLETED,
        result_summary="Inspection complete",
        changed_files=["file1.py"],
    )
    assert updated_node.status == ExecutionPlanNodeStatus.COMPLETED

    # start_attempt
    attempt = plan_repo.start_attempt(
        plan_id=plan.id,
        node_id="n1",
        effective_input_summary={"s": 1},
        effective_input_digest="dig-1",
        worker_type="codex",
        worker_profile="p1",
        runtime_mode="native",
        workspace_id="ws-1",
        task_trace_id="tr-1",
    )
    assert attempt.id is not None

    # finish_attempt
    finished = plan_repo.finish_attempt(
        attempt_id=attempt.id,
        status="completed",
        failure_kind=None,
        result_payload={"res": 1},
    )
    assert finished is not None
    assert finished.status == "completed"


def test_execution_plan_claim_activities(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="parent task 2")

    plan_repo = ExecutionPlanRepository(session)
    plan = plan_repo.create(task_id=task.id)
    _node = plan_repo.add_node(
        plan_id=plan.id,
        node_id="n1",
        goal="Inspect codebase",
        node_kind="inspect",
        depends_on=[],
        task_spec={"goal": "inspect"},
        status=ExecutionPlanNodeStatus.PENDING,
    )

    # claim_activity: new vs collision vs terminal_replay
    status1, att1 = plan_repo.claim_activity(
        plan_id=plan.id,
        node_id="n1",
        logical_activity_key="act-1",
        effective_input_summary={},
        effective_input_digest="dig-act",
        worker_type="codex",
        worker_profile=None,
        runtime_mode=None,
        workspace_id=None,
        task_trace_id=None,
    )
    assert status1 == "new"
    assert att1.claim_token is not None

    # heartbeat_activity
    assert plan_repo.heartbeat_activity(attempt_id=att1.id, claim_token=att1.claim_token) is True
    assert plan_repo.heartbeat_activity(attempt_id=att1.id, claim_token="bad-token") is False

    # collision
    status2, att2 = plan_repo.claim_activity(
        plan_id=plan.id,
        node_id="n1",
        logical_activity_key="act-1",
        effective_input_summary={},
        effective_input_digest="different-digest",
        worker_type="codex",
        worker_profile=None,
        runtime_mode=None,
        workspace_id=None,
        task_trace_id=None,
    )
    assert status2 == "collision"

    # terminal_replay
    plan_repo.finish_attempt(
        attempt_id=att1.id, status="completed", failure_kind=None, result_payload={"done": True}
    )
    status3, att3 = plan_repo.claim_activity(
        plan_id=plan.id,
        node_id="n1",
        logical_activity_key="act-1",
        effective_input_summary={},
        effective_input_digest="dig-act",
        worker_type="codex",
        worker_profile=None,
        runtime_mode=None,
        workspace_id=None,
        task_trace_id=None,
    )
    assert status3 == "terminal_replay"


def test_worker_run_repository_full_branches(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="run task")

    run_repo = WorkerRunRepository(session)
    now = datetime.now(UTC)
    _run = run_repo.create(
        task_id=task.id,
        worker_type="codex",
        workspace_id="ws-1",
        status="running",
        started_at=now,
    )

    metrics = run_repo.get_metrics(since=now - timedelta(hours=1))
    assert "worker_usage" in metrics


def test_user_and_session_repositories(session):
    user_repo = UserRepository(session)
    user = user_repo.create(external_user_id="ext-u1", display_name="User One")
    assert user.id is not None
    assert user_repo.get(user.id) == user
    assert user_repo.get_by_external_user_id("ext-u1") == user

    sess_repo = SessionRepository(session)
    sess = sess_repo.create(
        user_id=user.id,
        channel="telegram",
        external_thread_id="th-1",
    )
    assert sess.id is not None
    assert sess_repo.get(sess.id) == sess
    assert sess_repo.get_by_channel_thread(channel="telegram", external_thread_id="th-1") == sess
    assert sess_repo.list_by_user(user.id) == [sess]
    assert len(sess_repo.list_all(limit=10)) == 1

    updated_sess = sess_repo.set_active_task(session_id=sess.id, active_task_id="t1")
    assert updated_sess.active_task_id == "t1"
    assert sess_repo.set_active_task(session_id="nonexistent", active_task_id="t1") is None


def test_human_interaction_and_inbound_delivery_repositories(session, sample_user_and_session):
    user, conv = sample_user_and_session
    task_repo = TaskRepository(session)
    task = task_repo.create(session_id=conv.id, task_text="interactive task")

    hi_repo = HumanInteractionRepository(session)
    interactions = hi_repo.sync_task_spec_flags(
        task_id=task.id,
        task_spec={
            "requires_clarification": True,
            "clarification_questions": ["What scope?"],
            "requires_permission": True,
            "permission_reason": "High risk action",
        },
    )
    assert len(interactions) == 2

    pending = hi_repo.list_pending_with_task_context()
    assert len(pending) == 2

    # record response
    hi_id = interactions[0].id
    resp_hi, ok = hi_repo.record_response(
        hi_id, task_id=task.id, response_data={"answer": "Scope is auth module"}
    )
    assert ok is True
    assert resp_hi.status == HumanInteractionStatus.RESOLVED

    # Record response on non-pending returns False
    resp_hi2, ok2 = hi_repo.record_response(
        hi_id, task_id=task.id, response_data={"answer": "Again"}
    )
    assert ok2 is False

    # Inbound delivery
    id_repo = InboundDeliveryRepository(session)
    deliv = id_repo.create(channel="web", delivery_id="del-1")
    assert deliv.id is not None
    assert id_repo.get_by_channel_delivery(channel="web", delivery_id="del-1") == deliv

    attached = id_repo.attach_task_if_unassigned(
        channel="web", delivery_id="del-1", task_id=task.id
    )
    assert attached.task_id == task.id
    # Re-attach on already assigned returns None
    assert (
        id_repo.attach_task_if_unassigned(channel="web", delivery_id="del-1", task_id="t2") is None
    )


def test_observation_repository_full_branches(session, sample_user_and_session):
    user, conv = sample_user_and_session
    repo = ObservationRepository(session)

    obs = repo.create(
        source="agent",
        event_type="command_success",
        summary="Run pytest test suite",
        content="All tests passed successfully in 2 seconds.",
        session_id=conv.id,
    )
    assert obs.id is not None
    assert repo.get(obs.id) == obs

    timeline = repo.list_timeline(session_id=conv.id)
    assert len(timeline) == 1


def test_memory_proposal_repository_full(session):
    repo = MemoryProposalRepository(session)

    # invalid project without repo_url
    with pytest.raises(ValueError, match="repo_url is required"):
        repo.create(category=MemoryProposalCategory.PROJECT, memory_key="k1", value={"v": 1})

    # invalid personal with repo_url
    with pytest.raises(ValueError, match="repo_url must be omitted"):
        repo.create(
            category=MemoryProposalCategory.PERSONAL,
            memory_key="k1",
            value={"v": 1},
            repo_url="https://github.com/org/repo",
        )

    # valid personal proposal
    p_pers = repo.create(
        category=MemoryProposalCategory.PERSONAL, memory_key="k_pers", value={"pref": "dark"}
    )
    assert p_pers.id is not None
    assert repo.get(p_pers.id) == p_pers

    # valid project proposal
    p_proj = repo.create(
        category=MemoryProposalCategory.PROJECT,
        memory_key="k_proj",
        value={"arch": "monorepo"},
        repo_url="https://github.com/org/repo",
    )
    assert p_proj.id is not None

    # list with filters
    proposals = repo.list(
        status=MemoryProposalStatus.PENDING_REVIEW, category=MemoryProposalCategory.PERSONAL
    )
    assert len(proposals) == 1

    # accept personal proposal
    status1, p1, mem1, err1 = repo.accept(p_pers.id)
    assert status1 == "accepted"
    assert mem1 is not None

    # already accepted
    status2, p2, mem2, err2 = repo.accept(p_pers.id)
    assert status2 == "already_accepted"

    # reject project proposal
    status3, p3, err3 = repo.reject(p_proj.id)
    assert status3 == "rejected"

    # already rejected
    status4, p4, err4 = repo.reject(p_proj.id)
    assert status4 == "already_rejected"

    # conflict when accepting rejected
    status5, p5, mem5, err5 = repo.accept(p_proj.id)
    assert status5 == "conflict"

    # conflict when rejecting accepted
    status6, p6, err6 = repo.reject(p_pers.id)
    assert status6 == "conflict"

    # not found
    assert repo.accept("nonexistent")[0] == "not_found"
    assert repo.reject("nonexistent")[0] == "not_found"

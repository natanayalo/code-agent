# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from tests.unit.task_execution_service_support import *  # noqa: F403


def test_create_task_outcome_returns_existing_task_for_duplicate_delivery() -> None:
    """Duplicate delivery keys should resolve to the original task without new persistence."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Run the task service",
        session=execution_module.SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:42",
            external_thread_id="telegram:chat:100",
        ),
    )
    delivery_key = execution_module.DeliveryKey(channel="telegram", delivery_id="123")

    first = service.create_task_outcome(submission, delivery_key=delivery_key)
    second = service.create_task_outcome(submission, delivery_key=delivery_key)

    assert first.duplicate is False
    assert first.persisted is not None
    assert second.duplicate is True
    assert second.persisted is None
    assert second.task_snapshot.task_id == first.task_snapshot.task_id

    with session_scope(session_factory) as session:
        tasks = TaskRepository(session).list_by_session(first.task_snapshot.session_id)
        assert len(tasks) == 1


def test_create_task_outcome_logs_warning_on_duplicate_delivery(caplog) -> None:
    """Duplicate delivery should emit a structured warning log with delivery context."""
    import logging

    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Dedup log test",
        session=execution_module.SubmissionSession(
            channel="webhook",
            external_user_id="webhook:user:1",
            external_thread_id="webhook:thread:1",
        ),
    )
    delivery_key = execution_module.DeliveryKey(channel="webhook", delivery_id="dedup-key-abc")

    # First submission - should not warn
    service.create_task_outcome(submission, delivery_key=delivery_key)

    # Second submission - should trigger a warning
    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        service.create_task_outcome(submission, delivery_key=delivery_key)

    assert any(
        "Duplicate task delivery detected" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_create_task_outcome_recovers_stale_delivery_without_task_id() -> None:
    """A stale delivery claim without a linked task should be recoverable on retry."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        InboundDeliveryRepository(session).create(
            channel="telegram",
            delivery_id="stale-123",
            task_id=None,
        )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Recover stale delivery",
        session=execution_module.SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:42",
            external_thread_id="telegram:chat:100",
        ),
    )

    outcome = service.create_task_outcome(
        submission,
        delivery_key=execution_module.DeliveryKey(channel="telegram", delivery_id="stale-123"),
    )

    assert outcome.duplicate is False
    assert outcome.persisted is not None

    with session_scope(session_factory) as session:
        delivery = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="telegram",
            delivery_id="stale-123",
        )
        assert delivery is not None
        assert delivery.task_id == outcome.task_snapshot.task_id


def test_link_delivery_to_task_reraises_when_duplicate_row_disappears() -> None:
    """Delivery dedupe should re-raise if the conflicting row cannot be reloaded."""
    service, _ = _make_task_service()

    @contextmanager
    def _nested():
        yield

    class _MissingDeliveryRepo:
        def __init__(self) -> None:
            self.session = SimpleNamespace(begin_nested=_nested)

        def create(self, **kwargs) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

        def get_by_channel_delivery(self, **kwargs):
            return None

    with pytest.raises(IntegrityError):
        service._link_delivery_to_task(
            delivery_repo=_MissingDeliveryRepo(),
            delivery_key=execution_module.DeliveryKey(channel="telegram", delivery_id="gone"),
            task_id="task-1",
        )


def test_link_delivery_to_task_fails_when_retry_leaves_row_unassigned() -> None:
    """Delivery dedupe should fail loudly if retry still leaves the row without a task id."""
    service, _ = _make_task_service()

    @contextmanager
    def _nested():
        yield

    class _UnassignedDeliveryRepo:
        def __init__(self) -> None:
            self.session = SimpleNamespace(begin_nested=_nested)

        def create(self, **kwargs) -> None:
            raise IntegrityError("insert", {}, Exception("duplicate"))

        def get_by_channel_delivery(self, **kwargs):
            return SimpleNamespace(task_id=None)

        def attach_task_if_unassigned(self, **kwargs):
            return None

    with pytest.raises(RuntimeError, match="without a task_id after dedupe retry"):
        service._link_delivery_to_task(
            delivery_repo=_UnassignedDeliveryRepo(),
            delivery_key=execution_module.DeliveryKey(channel="telegram", delivery_id="stuck"),
            task_id="task-1",
        )


def test_create_task_persists_task_spec_human_interactions() -> None:
    """Task creation should project TaskSpec clarification/permission flags to interactions."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(task_text="debug this and drop table users")
    task_snapshot, _ = service.create_task(submission)

    with session_scope(session_factory) as session:
        interactions = HumanInteractionRepository(session).list_by_task(
            task_id=task_snapshot.task_id
        )

    assert task_snapshot.pending_interaction_count == 2
    assert len(task_snapshot.pending_interactions) == 2
    assert {interaction.interaction_type for interaction in task_snapshot.pending_interactions} == {
        "clarification",
        "permission",
    }

    assert len(interactions) == 2
    assert {interaction.interaction_type for interaction in interactions} == {
        HumanInteractionType.CLARIFICATION,
        HumanInteractionType.PERMISSION,
    }
    assert all(interaction.status is HumanInteractionStatus.PENDING for interaction in interactions)
    assert all(interaction.data["source"] == "task_spec" for interaction in interactions)


def test_get_task_sorts_pending_interactions_with_id_tiebreaker() -> None:
    """Pending interactions with equal timestamps should be ordered deterministically by id."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Implement deterministic ordering behavior")
    )

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        tie_time = utc_now()
        session.add_all(
            [
                HumanInteraction(
                    id="00000000-0000-0000-0000-000000000002",
                    task_id=task.id,
                    interaction_type=HumanInteractionType.CLARIFICATION,
                    status=HumanInteractionStatus.PENDING,
                    summary="Second by ID",
                    data={"source": "test"},
                    created_at=tie_time,
                    updated_at=tie_time,
                ),
                HumanInteraction(
                    id="00000000-0000-0000-0000-000000000001",
                    task_id=task.id,
                    interaction_type=HumanInteractionType.PERMISSION,
                    status=HumanInteractionStatus.PENDING,
                    summary="First by ID",
                    data={"source": "test"},
                    created_at=tie_time,
                    updated_at=tie_time,
                ),
            ]
        )
        session.flush()

    refreshed = service.get_task(task_snapshot.task_id)
    assert refreshed is not None
    assert refreshed.pending_interactions is not None
    inserted = [
        interaction
        for interaction in refreshed.pending_interactions
        if interaction.data.get("source") == "test"
    ]
    assert [interaction.interaction_id for interaction in inserted] == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]


def test_load_submission_for_task_restores_execution_overrides_and_budget() -> None:
    """Queued task loading should preserve worker/profile overrides plus constraints and budget."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Needs approval",
            repo_url="https://github.com/natanayalo/code-agent",
            worker_override="antigravity",
            worker_profile_override="antigravity-native-executor",
            constraints={"requires_approval": True, "approval_reason": "manual gate"},
            budget={"max_iterations": 5},
        )
    )

    loaded = service._load_submission_for_task(task_id=task_snapshot.task_id)
    assert loaded is not None
    submission, _ = loaded
    assert submission.worker_override == "antigravity"
    assert submission.worker_profile_override == "antigravity-native-executor"
    assert submission.constraints == {"requires_approval": True, "approval_reason": "manual gate"}
    assert submission.budget == {"max_iterations": 5}


def test_execution_boundary_models_trim_profile_overrides() -> None:
    """Task execution boundary models should trim canonical profile names."""

    submission = execution_module.TaskSubmission(
        task_text="Run task",
        worker_profile_override=" antigravity-native-executor ",
    )
    replay_request = execution_module.TaskReplayRequest(
        worker_profile_override=" antigravity-native-reviewer ",
    )

    assert submission.worker_profile_override == "antigravity-native-executor"
    assert replay_request.worker_profile_override == "antigravity-native-reviewer"


def test_execution_boundary_models_coerce_retired_gemini_inputs() -> None:
    """Task execution boundaries should coerce legacy Gemini names to Antigravity."""

    submission = execution_module.TaskSubmission(
        task_text="Run task",
        worker_override="gemini",
        worker_profile_override="gemini-native-executor",
    )
    assert submission.worker_override == "antigravity"
    assert submission.worker_profile_override == "antigravity-native-executor"


def test_load_submission_for_task_returns_none_when_persisted_scaffolding_is_missing() -> None:
    """Queued task reloads should fail closed when task, session, or user rows disappear."""
    service, session_factory = _make_task_service()

    assert service._load_submission_for_task(task_id="missing-task") is None

    missing_session_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Missing session",
            session=execution_module.SubmissionSession(
                channel="http",
                external_user_id="user-missing-session",
                external_thread_id="thread-missing-session",
            ),
        )
    )
    with session_scope(session_factory) as session:
        session.execute(
            text("DELETE FROM sessions WHERE id = :session_id"),
            {"session_id": missing_session_snapshot.session_id},
        )
    assert service._load_submission_for_task(task_id=missing_session_snapshot.task_id) is None

    missing_user_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Missing user",
            session=execution_module.SubmissionSession(
                channel="http",
                external_user_id="user-missing-user",
                external_thread_id="thread-missing-user",
            ),
        )
    )
    with session_scope(session_factory) as session:
        conversation_session = SessionRepository(session).get(missing_user_snapshot.session_id)
        assert conversation_session is not None
        session.execute(
            text("DELETE FROM users WHERE id = :user_id"),
            {"user_id": conversation_session.user_id},
        )
    assert service._load_submission_for_task(task_id=missing_user_snapshot.task_id) is None


def _setup_race_condition_mocks(
    monkeypatch,
    original_get_user,
    original_get_session,
):
    user_calls = 0
    session_calls = 0

    def stale_get_user(self, external_user_id: str):
        nonlocal user_calls
        user_calls += 1
        if user_calls == 1:
            return None
        return original_get_user(self, external_user_id)

    def stale_get_session(self, *, channel: str, external_thread_id: str):
        nonlocal session_calls
        session_calls += 1
        if session_calls == 1:
            return None
        return original_get_session(
            self,
            channel=channel,
            external_thread_id=external_thread_id,
        )

    monkeypatch.setattr(UserRepository, "get_by_external_user_id", stale_get_user)
    monkeypatch.setattr(SessionRepository, "get_by_channel_thread", stale_get_session)


def test_create_task_recovers_from_duplicate_user_and_session_race(monkeypatch) -> None:
    """Task creation should recover if another request inserts the user/session first."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        existing_user = user_repo.create(
            external_user_id="http:test-user",
            display_name="Existing User",
        )
        existing_session = session_repo.create(
            user_id=existing_user.id,
            channel="http",
            external_thread_id="thread-race",
        )

    _setup_race_condition_mocks(
        monkeypatch,
        original_get_user=UserRepository.get_by_external_user_id,
        original_get_session=SessionRepository.get_by_channel_thread,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    task_snapshot, persisted = service.create_task(
        execution_module.TaskSubmission(
            task_text="Recover from create race",
            repo_url="https://github.com/natanayalo/code-agent",
            session=execution_module.SubmissionSession(
                external_user_id="http:test-user",
                external_thread_id="thread-race",
            ),
        )
    )

    assert persisted.user_id == existing_user.id
    assert persisted.session_id == existing_session.id
    assert task_snapshot.status == TaskStatus.PENDING.value

    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        assert user_repo.get_by_external_user_id("http:test-user") is not None
        recovered_session = session_repo.get_by_channel_thread(
            channel="http",
            external_thread_id="thread-race",
        )
        assert recovered_session is not None
        assert len(session_repo.list_by_user(existing_user.id)) == 1
        assert len(task_repo.list_by_session(existing_session.id)) == 1


def test_create_task_persists_encryption_metadata() -> None:
    """Verify that TaskExecutionService correctly tags if secrets were encrypted at creation."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    # 1. Without encryption key
    with patch.dict("os.environ", {"CODE_AGENT_ENCRYPTION_KEY": ""}, clear=False):
        submission = execution_module.TaskSubmission(task_text="No encryption", secrets={"K": "V"})
        _, task_p = service.create_task(submission)

        with session_scope(session_factory) as session:
            reloaded = session.get(Task, task_p.task_id)
            assert reloaded is not None
            assert reloaded.secrets_encrypted is False

    # 2. With encryption key
    # Use a fresh service or ensure the decorator is re-initialized (since it's a
    # class/instance property)
    # Actually, EncryptedJSON reads os.environ in __init__.
    # A fresh service instantiation will trigger TaskRepository which initializes the model.
    key = Fernet.generate_key().decode()
    with patch.dict("os.environ", {"CODE_AGENT_ENCRYPTION_KEY": key}):
        # Mocking the is_active() call might be cleaner if we want to avoid complex re-init
        with patch.object(
            execution_module.Task.secrets.property.columns[0].type, "is_active", return_value=True
        ):
            submission = execution_module.TaskSubmission(
                task_text="With encryption", secrets={"K": "V"}
            )
            _, task_p = service.create_task(submission)

            with session_scope(session_factory) as session:
                reloaded = session.get(Task, task_p.task_id)
                assert reloaded is not None
                assert reloaded.secrets_encrypted is True


def test_map_task_to_summary_includes_trace_metadata(monkeypatch) -> None:
    """Task snapshots should include trace_id and trace_url when context is present."""
    engine = create_engine_from_url("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    # Enable tracing for URL generation
    execution_module._clear_tracing_config_cache()
    monkeypatch.setenv("CODE_AGENT_ENABLE_TRACING", "1")
    monkeypatch.setenv("CODE_AGENT_TRACING_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")

    with session_scope(session_factory) as session:
        user = User(external_user_id="user-1")
        session.add(user)
        session.flush()

        conv_session = ConversationSession(
            user_id=user.id, channel="test", external_thread_id="thread-1"
        )
        session.add(conv_session)
        session.flush()

        trace_context = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
        task = Task(
            session_id=conv_session.id,
            task_text="Test trace mapping",
            trace_context=trace_context,
            status=TaskStatus.PENDING,
        )
        session.add(task)
        session.flush()

        summary = service._map_task_to_summary(task)

        assert summary.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert (
            summary.trace_url
            == "http://localhost:6006/projects/code-agent/traces/4bf92f3577b34da6a3ce929d0e0e4736"
        )


def test_map_task_to_summary_omits_trace_metadata_when_disabled(monkeypatch) -> None:
    """Task snapshots should omit trace_url if tracing is disabled."""
    engine = create_engine_from_url("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    execution_module._clear_tracing_config_cache()
    monkeypatch.setenv("CODE_AGENT_ENABLE_TRACING", "0")

    with session_scope(session_factory) as session:
        user = User(external_user_id="user-1")
        session.add(user)
        session.flush()

        conv_session = ConversationSession(
            user_id=user.id, channel="test", external_thread_id="thread-1"
        )
        session.add(conv_session)
        session.flush()

        trace_context = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}
        task = Task(
            session_id=conv_session.id,
            task_text="Test trace mapping disabled",
            trace_context=trace_context,
            status=TaskStatus.PENDING,
        )
        session.add(task)
        session.flush()

        summary = service._map_task_to_summary(task)

        assert summary.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert summary.trace_url is None

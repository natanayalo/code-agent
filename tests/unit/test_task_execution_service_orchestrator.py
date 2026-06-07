# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from tests.unit.task_execution_service_support import *  # noqa: F403


def test_record_interaction_response_resolves_permission_and_emits_timeline() -> None:
    """Resolving a permission interaction should requeue the task and satisfy approval state."""
    service, session_factory = _make_task_service()
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Need elevated permission",
            constraints={"requires_approval": True},
        )
    )

    with session_scope(session_factory) as session:
        HumanInteractionRepository(session).sync_task_spec_flags(
            task_id=task_snapshot.task_id,
            task_spec={
                "requires_permission": True,
                "permission_reason": "Need workspace write access.",
                "risk_level": "high",
            },
        )
        permission_interaction = next(
            row
            for row in HumanInteractionRepository(session).list_by_task(
                task_id=task_snapshot.task_id
            )
            if row.interaction_type is HumanInteractionType.PERMISSION
        )

    refreshed = service.record_interaction_response(
        task_snapshot.task_id,
        permission_interaction.id,
        execution_module.InteractionResponse(response_data={"approved": True}),
    )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING.value

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is not None
        constraints = dict(task.constraints or {})
        assert constraints["requires_approval"] is False
        assert constraints["approval"]["status"] == "approved"
        assert constraints["approval"]["source"] == "orchestrator"
        assert len(constraints["interactions"]) == 1
        interaction_payload = next(iter(constraints["interactions"].values()))
        assert interaction_payload["status"] == "resolved"
        assert interaction_payload["response_data"] == {"approved": True}
        timeline = service.get_task(task_snapshot.task_id).timeline
        assert any(event.event_type == "approval_granted" for event in timeline)


def test_record_interaction_response_handles_missing_and_already_terminal_rows() -> None:
    """Interaction responses should fail closed for missing tasks and be idempotent."""
    service, session_factory = _make_task_service()
    assert (
        service.record_interaction_response(
            "missing-task",
            "missing-interaction",
            execution_module.InteractionResponse(response_data={"answer": "none"}),
        )
        is None
    )

    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Already answered interaction")
    )
    with session_scope(session_factory) as session:
        original_task = TaskRepository(session).get(task_snapshot.task_id)
        assert original_task is not None
        original_next_attempt_at = original_task.next_attempt_at
    with session_scope(session_factory) as session:
        interaction = HumanInteraction(
            task_id=task_snapshot.task_id,
            interaction_type=HumanInteractionType.CLARIFICATION,
            status=HumanInteractionStatus.RESOLVED,
            summary="Already resolved",
            data={"source": "task_spec", "resume_token": "clarification"},
            response_data={"answer": "done"},
        )
        session.add(interaction)
        session.flush()
        interaction_id = interaction.id

    refreshed = service.record_interaction_response(
        task_snapshot.task_id,
        interaction_id,
        execution_module.InteractionResponse(response_data={"answer": "done"}),
    )

    assert refreshed is not None
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        assert dict(task.constraints or {}) == {}
        assert task.next_attempt_at == original_next_attempt_at


def test_get_operational_metrics_returns_service_aggregation() -> None:
    """Direct service metrics should aggregate task and run state consistently."""
    service, session_factory = _make_task_service()
    now = utc_now()

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        run_repo = WorkerRunRepository(session)

        completed = task_repo.create(
            session_id="session-1",
            task_text="done",
            status=TaskStatus.COMPLETED,
        )
        completed.attempt_count = 2
        failed = task_repo.create(
            session_id="session-1",
            task_text="failed",
            status=TaskStatus.FAILED,
        )
        failed.attempt_count = 1
        session.flush()

        run_repo.create(
            task_id=completed.id,
            worker_type=WorkerType.CODEX,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            started_at=now - timedelta(seconds=20),
            finished_at=now - timedelta(seconds=10),
            status=WorkerRunStatus.SUCCESS,
        )
        run_repo.create(
            task_id=failed.id,
            worker_type=WorkerType.GEMINI,
            runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
            started_at=now - timedelta(seconds=30),
            finished_at=now - timedelta(seconds=5),
            status=WorkerRunStatus.FAILURE,
        )

    metrics = service.get_operational_metrics(window_hours=0)

    assert metrics.total_tasks == 2
    assert metrics.retried_tasks == 1
    assert metrics.retry_rate == 0.5
    assert metrics.status_counts["completed"] == 1
    assert metrics.worker_usage == {"codex": 1, "gemini": 1}
    assert metrics.runtime_mode_usage == {"native_agent": 1, "tool_loop": 1}
    assert metrics.legacy_tool_loop_usage == {"gemini": 1}
    assert metrics.avg_duration_seconds == 17.5
    assert metrics.success_rate == 0.5


def test_replay_task_returns_not_found_when_source_submission_cannot_be_reloaded(
    monkeypatch,
) -> None:
    """Replay should fail clearly when the source task exists but its session context is missing."""
    service, session_factory = _make_task_service()
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="Replay me"))
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(task_id=snapshot.task_id, status=TaskStatus.COMPLETED)

    monkeypatch.setattr(service, "_load_submission_for_task", lambda task_id: None)

    result = service.replay_task(source_task_id=snapshot.task_id)

    assert result.status == "not_found"
    assert result.task_snapshot is None
    assert "could not be resolved for replay" in (result.detail or "")


def test_task_execution_service_reuses_one_compiled_graph(
    monkeypatch,
) -> None:
    """The execution service should compile its graph once and reuse it across tasks."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    build_calls: list[Worker] = []

    def fake_build_orchestrator_graph(
        *, worker: Worker, gemini_worker=None, **kwargs
    ) -> _FakeGraph:
        build_calls.append(worker)
        return fake_graph

    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        fake_build_orchestrator_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    submission = execution_module.TaskSubmission(
        task_text="Run the task service",
        repo_url="https://github.com/natanayalo/code-agent",
    )

    _, persisted_one = service.create_task(submission)
    _, persisted_two = service.create_task(submission)

    asyncio.run(service._run_orchestrator(submission, persisted_one))
    asyncio.run(service._run_orchestrator(submission, persisted_two))

    assert len(build_calls) == 1
    assert len(fake_graph.calls) == 2


def test_task_execution_service_passes_orchestrator_brain_to_graph_builder(monkeypatch) -> None:
    """Graph construction should receive the configured orchestrator-brain provider."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    seen_brains: list[object | None] = []

    def fake_build_orchestrator_graph(*, orchestrator_brain=None, **kwargs):
        del kwargs
        seen_brains.append(orchestrator_brain)
        return fake_graph

    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        fake_build_orchestrator_graph,
    )

    brain = RuleBasedOrchestratorBrain()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        orchestrator_brain=brain,
    )

    submission = execution_module.TaskSubmission(task_text="route with brain")
    _, persisted = service.create_task(submission)
    asyncio.run(service._run_orchestrator(submission, persisted))

    assert seen_brains == [brain]


def test_run_orchestrator_propagates_submission_secrets(
    monkeypatch,
) -> None:
    """The execution service must include submission secrets in the orchestrator payload."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        lambda *, worker, gemini_worker=None, **kwargs: fake_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    submission = execution_module.TaskSubmission(
        task_text="Run with secrets",
        secrets={"TEST_SECRET": "test-value"},
    )

    _, persisted = service.create_task(submission)
    asyncio.run(service._run_orchestrator(submission, persisted))

    assert len(fake_graph.calls) == 1
    task_payload = fake_graph.calls[0]["task"]
    assert task_payload["secrets"] == {"TEST_SECRET": "test-value"}


def test_run_orchestrator_applies_effective_budget_policy_to_payload(monkeypatch) -> None:
    """Orchestrator payload should include mode defaults and global caps."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        lambda *, worker, gemini_worker=None, **kwargs: fake_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Run with oversized budget",
        budget={"max_iterations": 1000, "max_tool_calls": 1000},
        session=execution_module.SubmissionSession(
            channel="webhook:ci",
            external_user_id="webhook:ci:user-1",
            external_thread_id="thread-1",
        ),
    )

    _, persisted = service.create_task(submission)
    asyncio.run(service._run_orchestrator(submission, persisted))

    assert len(fake_graph.calls) == 1
    task_payload = fake_graph.calls[0]["task"]
    assert task_payload["budget"]["execution_mode"] == "unattended"
    assert task_payload["budget"]["max_iterations"] == 20
    assert task_payload["budget"]["worker_timeout_seconds"] == 300
    assert task_payload["budget"]["max_tool_calls"] == 100


def test_run_orchestrator_handles_missing_opentelemetry_import(monkeypatch) -> None:
    """Orchestrator execution should stay functional when optional OTel import is unavailable."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        lambda *, worker, gemini_worker=None, **kwargs: fake_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Run without opentelemetry dependency",
        repo_url="https://github.com/natanayalo/code-agent",
    )

    _, persisted = service.create_task(submission)
    real_import = builtins.__import__

    def _import_without_otel(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "opentelemetry":
            raise ImportError("opentelemetry unavailable")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=_import_without_otel):
        state = asyncio.run(service._run_orchestrator(submission, persisted))

    assert state.result is not None
    assert state.result.status == "success"
    assert len(fake_graph.calls) == 1


def test_load_submission_for_task_recovers_secrets() -> None:
    """The submission reconstruction logic must restore secrets from the persisted Task record."""
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
        task_text="Recoverable secrets",
        secrets={"PERSISTED_SECRET": "stored-value"},
    )
    _, persisted = service.create_task(submission)

    # Reload from database.
    reloaded_result = service._load_submission_for_task(task_id=persisted.task_id)
    assert reloaded_result is not None
    reloaded_submission, _ = reloaded_result

    assert reloaded_submission.secrets == {"PERSISTED_SECRET": "stored-value"}


def test_replay_task_replaces_secrets_instead_of_merging() -> None:
    """Replaying a task with new secrets must fully replace the old set to prevent leakage."""
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

    # Initial task with secret A
    submission = execution_module.TaskSubmission(
        task_text="Original task", secrets={"KEY_A": "VAL_A"}
    )
    _, original_persisted = service.create_task(submission)

    # Mark as completed so it's replayable
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(
            task_id=original_persisted.task_id, status=TaskStatus.COMPLETED
        )

    # Replay with secret B (should remove A)
    replay_request = execution_module.TaskReplayRequest(secrets={"KEY_B": "VAL_B"})
    replay_outcome = service.replay_task(
        source_task_id=original_persisted.task_id,
        replay_request=replay_request,
    )

    assert replay_outcome.status == "created"
    assert replay_outcome.task_snapshot is not None
    new_task_id = replay_outcome.task_snapshot.task_id

    # Verify replayed task has only B
    reloaded_result = service._load_submission_for_task(task_id=new_task_id)
    assert reloaded_result is not None
    reloaded_submission, _ = reloaded_result

    assert reloaded_submission.secrets == {"KEY_B": "VAL_B"}
    assert "KEY_A" not in reloaded_submission.secrets


def test_normalize_orchestrator_output_converts_interrupts_to_failure_result() -> None:
    """Unresolved graph interrupts should be converted into a persistable failure shape."""
    raw_output = {
        "task": {"task_text": "Delete files"},
        "__interrupt__": [
            {
                "value": {
                    "approval_type": "permission_escalation",
                    "requested_permission": "dangerous_shell",
                    "reason": "Worker requested elevated permission.",
                }
            }
        ],
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)

    assert isinstance(normalized, dict)
    assert "__interrupt__" not in normalized
    state = OrchestratorState.model_validate(normalized)
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert state.result.requested_permission == "dangerous_shell"
    assert "permission escalation approval" in (state.result.summary or "")
    assert "orchestrator interrupted awaiting manual approval" in state.errors


def test_summarize_graph_span_input_emits_compact_context() -> None:
    """Graph span input summary should retain only stable routing/execution identifiers."""
    graph_input = {
        "session": {
            "session_id": "sess-1",
            "channel": "webhook:test",
        },
        "task": {
            "task_id": "task-1",
            "branch": "task/branch",
            "constraints": {"execution_mode": "unattended"},
            "budget": {"max_iterations": 3},
        },
        "task_spec": {"task_type": "refactor"},
        "attempt_count": 2,
    }

    summary = execution_module._summarize_graph_span_input(graph_input)

    assert summary == {
        "task_id": "task-1",
        "attempt_count": 2,
        "channel": "webhook:test",
        "branch": "task/branch",
        "task_type": "refactor",
        "execution_mode": "unattended",
        "max_iterations": 3,
    }


def test_summarize_graph_span_output_emits_compact_result_status() -> None:
    """Graph span output summary should avoid dumping full orchestration payload."""
    raw_output = {
        "current_step": "persist_memory",
        "attempt_count": 2,
        "timeline_persisted_count": 12,
        "repair_handoff_requested": False,
        "result": {"status": "success"},
        "review": {"outcome": "no_findings"},
        "verification": {"status": "warning"},
        "errors": ["foo", "bar"],
    }

    summary = execution_module._summarize_graph_span_output(raw_output)

    assert summary == {
        "current_step": "persist_memory",
        "attempt_count": 2,
        "timeline_persisted_count": 12,
        "repair_handoff_requested": False,
        "result_status": "success",
        "review_outcome": "no_findings",
        "verification_status": "warning",
        "error_count": 2,
    }


def test_graph_payload_helpers_cover_models_and_delivery_contract_edges() -> None:
    """Graph payload helpers should normalize models, malformed outputs, and delivery checks."""
    model_payload = execution_module._extract_graph_payload(TaskRequest(task_text="Model payload"))
    assert model_payload["task_text"] == "Model payload"
    assert execution_module._extract_graph_payload(42) == {}
    assert execution_module._summarize_graph_span_output(42) == {"output_type": "int"}

    summary = execution_module._summarize_graph_span_output(
        {
            "task": {
                "constraints": {
                    "interactions": {
                        "skip": "not-a-mapping",
                        "first": {"interaction_type": "clarification", "status": "pending"},
                        "second": {"interaction_type": "clarification", "status": "resolved"},
                    }
                }
            },
            "verification": {
                "status": "failed",
                "failure_kind": "delivery_contract",
                "items": [
                    "ignore-me",
                    {
                        "label": "file_changes",
                        "status": "failed",
                        "reason_code": "incomplete_delivery",
                    },
                ],
            },
        }
    )

    assert summary == {
        "verification_status": "failed",
        "verifier_failure_kind": "delivery_contract",
        "clarification_round": 2,
        "clarification_resolved": True,
        "delivery_contract_passed": False,
    }


def test_summarize_graph_span_output_marks_delivery_contract_passed_for_warning_items() -> None:
    """Delivery-contract verification should pass for warning/passed file-change checks."""
    summary = execution_module._summarize_graph_span_output(
        {
            "verification": {
                "items": [
                    {
                        "label": "file_changes",
                        "status": "warning",
                    }
                ]
            }
        }
    )

    assert summary == {"delivery_contract_passed": True}


def test_normalize_orchestrator_output_canonicalizes_requested_permission() -> None:
    """Interrupt permission payloads should be normalized to explicit permission classes."""
    raw_output = {
        "task": {"task_text": "Fetch dependency"},
        "__interrupt__": [
            {
                "value": {
                    "approval_type": "permission_escalation",
                    "requested_permission": "  Networked_Write  ",
                    "reason": "Network install required.",
                }
            }
        ],
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    state = OrchestratorState.model_validate(normalized)

    assert state.result is not None
    assert state.result.requested_permission == "networked_write"
    assert "networked_write" in (state.result.summary or "")


def test_normalize_orchestrator_output_drops_unknown_requested_permission() -> None:
    """Unknown permission values should fail closed and not be persisted as requested permission."""
    raw_output = {
        "task": {"task_text": "Fetch dependency"},
        "__interrupt__": [
            {
                "value": {
                    "approval_type": "permission_escalation",
                    "requested_permission": "network_write",
                    "reason": "Network install required.",
                }
            }
        ],
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    state = OrchestratorState.model_validate(normalized)

    assert state.result is not None
    assert state.result.requested_permission is None
    assert "permission escalation approval" in (state.result.summary or "")
    assert "network_write" not in (state.result.summary or "")


def test_normalize_orchestrator_output_canonicalizes_existing_result_permission() -> None:
    """Existing result payloads should also normalize requested_permission to canonical classes."""
    raw_output = {
        "task": {"task_text": "Fetch dependency"},
        "result": {
            "status": "failure",
            "summary": "permission requested",
            "requested_permission": "  Networked_Write ",
            "commands_run": [],
            "files_changed": [],
            "test_results": [],
            "artifacts": [],
        },
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    assert isinstance(normalized, dict)
    assert normalized["result"]["requested_permission"] == "networked_write"


def test_normalize_orchestrator_output_drops_unknown_existing_result_permission() -> None:
    """Non-canonical requested_permission values in existing results should fail closed."""
    raw_output = {
        "task": {"task_text": "Fetch dependency"},
        "result": {
            "status": "failure",
            "summary": "permission requested",
            "requested_permission": "network_write",
            "commands_run": [],
            "files_changed": [],
            "test_results": [],
            "artifacts": [],
        },
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    assert isinstance(normalized, dict)
    assert normalized["result"]["requested_permission"] is None


def test_normalize_orchestrator_output_canonicalizes_existing_result_model_permission() -> None:
    """Normalization should also run when `result` is provided as a Pydantic model."""
    raw_output = {
        "task": {"task_text": "Fetch dependency"},
        "result": WorkerResult(
            status="failure",
            summary="permission requested",
            requested_permission="  Networked_Write ",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        ),
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    assert isinstance(normalized, dict)
    assert normalized["result"]["requested_permission"] == "networked_write"


def test_normalize_orchestrator_output_canonicalizes_when_raw_output_is_base_model() -> None:
    """Normalization should run when the entire raw output is a Pydantic model."""
    raw_output = OrchestratorState(
        task={"task_text": "Fetch dependency"},
        result=WorkerResult(
            status="failure",
            summary="permission requested",
            requested_permission="  Networked_Write ",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        ),
    )

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    assert isinstance(normalized, dict)
    assert normalized["result"]["requested_permission"] == "networked_write"


def test_normalize_orchestrator_output_preserves_interrupts_from_base_model_attributes() -> None:
    """Interrupt metadata attached to model instances should survive normalization."""
    raw_output = OrchestratorState(task={"task_text": "Delete files"})
    object.__setattr__(
        raw_output,
        "__interrupt__",
        [
            {
                "value": {
                    "approval_type": "permission_escalation",
                    "requested_permission": "  Networked_Write ",
                }
            }
        ],
    )

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    assert isinstance(normalized, dict)
    assert "__interrupt__" not in normalized

    state = OrchestratorState.model_validate(normalized)
    assert state.result is not None
    assert state.result.status == "failure"
    assert state.result.requested_permission == "networked_write"
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert "orchestrator interrupted awaiting manual approval" in state.errors


def test_normalize_orchestrator_output_formats_manual_approval_summary_without_duplication() -> (
    None
):
    """Manual approval summaries should not contain duplicated 'approval' wording."""
    raw_output = {
        "task": {"task_text": "Delete files"},
        "__interrupt__": [
            {
                "value": {
                    "approval_type": "manual_approval",
                    "reason": "Manual approval required for this task.",
                }
            }
        ],
    }

    normalized = execution_module._normalize_orchestrator_graph_output(raw_output)
    state = OrchestratorState.model_validate(normalized)

    assert state.result is not None
    assert state.result.summary is not None
    assert "manual approval approval" not in state.result.summary.lower()
    assert "manual approval required" in state.result.summary.lower()

# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from tests.unit.task_execution_service_support import *  # noqa: F403


def _make_base_orchestrator_state(persisted, submission, **kwargs):
    base_state = dict(
        current_step="persist_memory",
        session=SessionRef(
            session_id=persisted.session_id,
            user_id=persisted.user_id,
            channel=persisted.channel,
            external_thread_id=persisted.external_thread_id,
            active_task_id=persisted.task_id,
            status="active",
        ),
        task=TaskRequest(
            task_id=persisted.task_id,
            task_text=submission.task_text,
            repo_url=submission.repo_url,
            branch=submission.branch,
            priority=submission.priority,
            worker_override=submission.worker_override,
            constraints=dict(submission.constraints),
            budget=dict(submission.budget),
        ),
        normalized_task_text=submission.task_text,
        task_kind="implementation",
        memory=MemoryContext(),
        route=RouteDecision(
            chosen_worker="codex",
            route_reason="cheap_mechanical_change",
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(),
        dispatch=WorkerDispatch(worker_type="codex"),
        result=None,
    )
    base_state.update(kwargs)
    return OrchestratorState(**base_state)


def _make_review_result(reviewer_kind, summary, title, category, file_path, line_start):
    return ReviewResult(
        reviewer_kind=reviewer_kind,
        summary=summary,
        confidence=0.8,
        outcome="findings",
        findings=[
            ReviewFinding(
                severity="low",
                category=category,
                confidence=0.8,
                file_path=file_path,
                line_start=line_start,
                line_end=line_start,
                title=title,
                why_it_matters="Ensures review artifacts are persisted.",
            )
        ],
    )


def _setup_persistence_test_db():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _create_retained_worker_run(worker_run_repo, task_id, session_id, workspace_path):
    return worker_run_repo.create(
        task_id=task_id,
        session_id=session_id,
        worker_type=WorkerType.CODEX,
        workspace_id=workspace_path.name,
        started_at=utc_now() - timedelta(minutes=5),
        finished_at=utc_now() - timedelta(minutes=4),
        retention_expires_at=utc_now() - timedelta(minutes=1),
        status=WorkerRunStatus.SUCCESS,
        summary="completed",
        commands_run=[],
        files_changed_count=0,
        artifact_index=[
            {
                "name": "workspace",
                "uri": str(workspace_path),
                "artifact_type": "workspace",
            }
        ],
    )


def test_create_task_pins_selected_orchestration_runtime(monkeypatch) -> None:
    """Runtime selection is persisted once instead of being re-evaluated during execution."""
    session_factory = _setup_persistence_test_db()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")

    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Pin Temporal runtime")
    )

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "legacy")
    reloaded_snapshot = service.get_task(task_snapshot.task_id)
    assert task_snapshot.orchestration_runtime == "temporal"
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.orchestration_runtime == "temporal"


def test_temporal_task_creation_persists_a_start_command_with_the_task(monkeypatch) -> None:
    """A crash after commit leaves durable work for the worker dispatcher to reconcile."""
    session_factory = _setup_persistence_test_db()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")

    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="Start durably"))

    from db.models import TemporalCommand

    with session_scope(session_factory) as session:
        command = session.query(TemporalCommand).one()
        assert command.task_id == snapshot.task_id
        assert command.command_type == "start"
        assert command.command_key == f"task:{snapshot.task_id}:start"
        assert command.delivered_at is None


def test_temporal_availability_retries_then_allows_a_recovered_submission(monkeypatch) -> None:
    """A transient outage should not require restarting the API process."""
    session_factory = _setup_persistence_test_db()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    attempts: list[str] = []

    async def connect(address: str):
        attempts.append(address)
        if len(attempts) == 1:
            raise ConnectionError("Temporal unavailable")
        return object()

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    from temporalio.client import Client

    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(execution_module.time, "sleep", lambda _seconds: None)

    service.ensure_temporal_available()

    assert attempts == ["localhost:7233", "localhost:7233"]


def test_persist_execution_outcome_creates_error_worker_run_without_result() -> None:
    """Missing worker results should still leave an error worker-run record for observability."""
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist an error run",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = _make_base_orchestrator_state(persisted, submission)

    started_at = datetime.now()
    finished_at = datetime.now()
    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.status == TaskStatus.FAILED.value
    assert task_snapshot.chosen_worker == "codex"
    assert task_snapshot.route_reason == "cheap_mechanical_change"
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.session_id == persisted.session_id
    assert task_snapshot.latest_run.status == WorkerRunStatus.ERROR.value
    assert task_snapshot.latest_run.summary == "Worker did not return a result."
    assert task_snapshot.latest_run.verifier_outcome is None
    assert task_snapshot.latest_run.artifact_index == []
    assert task_snapshot.latest_run.files_changed_count == 0


def test_persist_execution_outcome_persists_delivery_metadata() -> None:
    """Delivery metadata should survive run persistence and snapshot mapping."""
    session_factory = _setup_persistence_test_db()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist delivery metadata",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)
    delivery_metadata = {
        "delivery_mode": "draft_pr",
        "branch_name": "task/test-ci",
        "pr_url": "https://github.com/natanayalo/code-agent/pull/123",
        "pr_number": 123,
        "head_sha": "abc123",
        "ci_status": "pending",
        "ci_failed_jobs": [],
    }
    state = _make_base_orchestrator_state(
        persisted,
        submission,
        result=WorkerResult(
            status="success",
            summary="done",
            delivery_metadata=delivery_metadata,
        ),
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.delivery_metadata is not None
    assert task_snapshot.latest_run.delivery_metadata.pr_url == delivery_metadata["pr_url"]
    assert task_snapshot.latest_run.delivery_metadata.head_sha == "abc123"


def test_retention_cleanup_clears_workspace_files_and_persisted_artifacts(
    tmp_path: Path,
) -> None:
    """Expired retained runs should remove both DB artifacts and the workspace on disk."""
    session_factory = _setup_persistence_test_db()

    workspace_root = tmp_path / "workspaces"
    workspace_path = workspace_root / "workspace-retained"
    artifact_path = workspace_path / "artifacts" / "command-123"
    artifact_path.mkdir(parents=True)
    (artifact_path / "stdout.log").write_text("old stdout\n", encoding="utf-8")
    scratch_path = workspace_root / ".code-agent-scratch" / workspace_path.name
    (scratch_path / "node" / "stdout.log").parent.mkdir(parents=True)
    (scratch_path / "node" / "stdout.log").write_text("old scratch\n", encoding="utf-8")

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        workspace_root=workspace_root,
        retention_seconds=60,
    )
    submission = execution_module.TaskSubmission(
        task_text="Retain then prune",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        task = task_repo.get(persisted.task_id)
        assert task is not None

        worker_run = _create_retained_worker_run(
            worker_run_repo, task.id, persisted.session_id, workspace_path
        )
        artifact_repo.create(
            run_id=worker_run.id,
            artifact_type="workspace",
            name="workspace",
            uri=str(workspace_path),
        )

    assert workspace_path.exists()

    pruned = service._prune_retained_runs(now=utc_now())

    assert pruned == 1
    assert not workspace_path.exists()
    assert not scratch_path.exists()

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.artifacts == []
    assert task_snapshot.latest_run.artifact_index == []

    with session_scope(session_factory) as session:
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        worker_runs = worker_run_repo.list_by_task(task_snapshot.task_id)
        assert len(worker_runs) == 1
        assert worker_runs[0].artifact_index == []
        assert worker_runs[0].retention_expires_at is None
        assert artifact_repo.list_by_run(worker_runs[0].id) == []


def test_persist_execution_outcome_falls_back_to_route_worker_when_dispatch_missing() -> None:
    """Persisted runs should still be written when dispatch worker metadata is absent."""
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist interrupted run",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = _make_base_orchestrator_state(
        persisted,
        submission,
        current_step="await_approval",
        approval=ApprovalCheckpoint(
            required=True,
            status="pending",
            approval_type="permission_escalation",
        ),
        dispatch=WorkerDispatch(worker_type=None),
        result=WorkerResult(
            status="failure",
            summary="Run paused pending permission escalation approval.",
            requested_permission="workspace_write",
            next_action_hint="await_manual_follow_up",
        ),
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        force_task_status=TaskStatus.FAILED,
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.status == TaskStatus.FAILED.value
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.worker_type == WorkerType.CODEX.value
    assert task_snapshot.latest_run.status == WorkerRunStatus.FAILURE.value
    assert task_snapshot.latest_run.requested_permission == "workspace_write"


def test_persist_execution_outcome_persists_session_state_update() -> None:
    """Execution persistence should store the compact session working state."""
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist session state",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = _make_base_orchestrator_state(
        persisted,
        submission,
        result=WorkerResult(
            status="success",
            summary="done",
            requested_permission="workspace_write",
            budget_usage={"iterations_used": 1, "tool_calls_used": 1},
            files_changed=["orchestrator/execution.py"],
        ),
        verification={
            "status": "passed",
            "summary": "Verifier accepted the run.",
            "items": [{"label": "worker_status", "status": "passed"}],
        },
        session_state_update={
            "active_goal": "Persist session state",
            "decisions_made": {"worker": "codex"},
            "identified_risks": {"network": "restricted"},
            "files_touched": ["orchestrator/execution.py"],
        },
    )

    started_at = datetime.now()
    finished_at = datetime.now()
    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.requested_permission == "workspace_write"
    assert task_snapshot.latest_run.budget_usage == {
        "iterations_used": 1,
        "tool_calls_used": 1,
    }
    assert task_snapshot.latest_run.verifier_outcome == {
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [
            {
                "id": "v-0-worker_status-passed",
                "label": "worker_status",
                "status": "passed",
                "message": None,
            }
        ],
    }

    with session_scope(session_factory) as session:
        session_state_repo = SessionStateRepository(session)
        session_state = session_state_repo.get(persisted.session_id)

        assert session_state is not None
        assert session_state.active_goal == "Persist session state"
        assert session_state.decisions_made == {"worker": "codex"}
        assert session_state.identified_risks == {"network": "restricted"}
        assert session_state.files_touched == ["orchestrator/execution.py"]


def test_persist_execution_outcome_persists_structured_review_result_artifact() -> None:
    """Structured review output should be persisted as a dedicated run artifact."""
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist review artifact",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = _make_base_orchestrator_state(
        persisted,
        submission,
        result=WorkerResult(
            status="success",
            summary="done",
            review_result=_make_review_result(
                "worker_self_review",
                "One issue found in changed logic.",
                "Missing empty-result guard",
                "logic",
                "workers/codex_cli_worker.py",
                120,
            ),
        ),
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    review_entries = [
        artifact
        for artifact in task_snapshot.latest_run.artifact_index
        if artifact.get("artifact_type") == ArtifactType.REVIEW_RESULT.value
    ]
    assert len(review_entries) == 1
    review_payload = review_entries[0]["artifact_metadata"]["review_result"]
    assert review_payload["outcome"] == "findings"
    assert review_payload["findings"][0]["file_path"] == "workers/codex_cli_worker.py"
    assert review_payload["findings"][0]["line_start"] == 120

    persisted_artifacts = [
        artifact
        for artifact in task_snapshot.latest_run.artifacts
        if artifact.artifact_type == ArtifactType.REVIEW_RESULT.value
    ]
    assert len(persisted_artifacts) == 1
    assert persisted_artifacts[0].artifact_metadata == {"review_result": review_payload}


def test_persist_execution_outcome_persists_worker_and_independent_review_artifacts() -> None:
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist review artifacts",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)
    assert persisted is not None

    state = _make_base_orchestrator_state(
        persisted,
        submission,
        result=WorkerResult(
            status="success",
            summary="done",
            review_result=_make_review_result(
                "worker_self_review",
                "self review",
                "Self review finding",
                "tests",
                "tests/unit/test_task_execution_service.py",
                1,
            ),
        ),
        review=_make_review_result(
            "independent_reviewer",
            "independent review",
            "Independent review finding",
            "correctness",
            "orchestrator/review.py",
            1,
        ),
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None

    artifact_types = {
        artifact["artifact_type"] for artifact in task_snapshot.latest_run.artifact_index
    }
    assert ArtifactType.REVIEW_RESULT.value in artifact_types
    assert ArtifactType.INDEPENDENT_REVIEW_RESULT.value in artifact_types

    persisted_types = {artifact.artifact_type for artifact in task_snapshot.latest_run.artifacts}
    assert ArtifactType.REVIEW_RESULT.value in persisted_types
    assert ArtifactType.INDEPENDENT_REVIEW_RESULT.value in persisted_types


def test_serialize_review_result_mapping_recursively_normalizes_nested_models() -> None:
    """Raw mapping payloads with nested models should be JSON-serializable."""
    serialized = execution_module._serialize_review_result(
        {
            "reviewer_kind": "worker_self_review",
            "summary": "Issue found.",
            "confidence": 0.7,
            "outcome": "findings",
            "findings": [
                ReviewFinding(
                    severity="low",
                    category="tests",
                    confidence=0.7,
                    file_path="tests/unit/test_task_execution_service.py",
                    line_start=1,
                    line_end=1,
                    title="Example finding",
                    why_it_matters="Ensures nested model serialization is robust.",
                )
            ],
        }
    )

    assert serialized is not None
    assert serialized["findings"][0]["title"] == "Example finding"
    assert serialized["findings"][0]["line_start"] == 1


def test_persist_execution_outcome_accepts_raw_verification_mapping() -> None:
    """Execution persistence should tolerate verification payloads that are plain dicts."""
    session_factory = _setup_persistence_test_db()

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Persist raw verification mapping",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = _make_base_orchestrator_state(persisted, submission)
    state.result = WorkerResult(status="success", summary="done")
    state.verification = {  # type: ignore
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [],
    }

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.verifier_outcome == {
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [],
    }


def test_workspace_id_from_artifacts_supports_url_and_custom_workspace_uris() -> None:
    """Workspace ids should still be inferred when artifact URIs are not plain local paths."""
    assert (
        execution_module._workspace_id_from_artifacts(
            [
                ArtifactReference(
                    name="workspace",
                    uri="https://artifacts.example.com/runs/workspace-1234?signature=abc",
                    artifact_type="workspace",
                )
            ]
        )
        == "workspace-1234"
    )
    assert (
        execution_module._workspace_id_from_artifacts(
            [
                ArtifactReference(
                    name="workspace",
                    uri="workspace://workspace-5678",
                    artifact_type="workspace",
                )
            ]
        )
        == "workspace-5678"
    )

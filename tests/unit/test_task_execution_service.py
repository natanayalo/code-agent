"""Unit tests for the task execution service."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    WorkerRunStatus,
    WorkerType,
)
from db.models import HumanInteraction
from orchestrator import (
    ApprovalCheckpoint,
    MemoryContext,
    OrchestratorState,
    RouteDecision,
    SessionRef,
    TaskRequest,
    WorkerDispatch,
    WorkerResult,
)
from orchestrator import execution as execution_module
from repositories import (
    ArtifactRepository,
    HumanInteractionRepository,
    InboundDeliveryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    UserRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import ArtifactReference, ReviewFinding, ReviewResult, Worker, WorkerRequest


class _StaticWorker(Worker):
    """Minimal worker double used to initialize the service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


class _FakeGraph:
    """Graph double that records invocations and returns a valid final state."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        self.calls.append(payload)
        session = SessionRef.model_validate(payload["session"])
        task = TaskRequest.model_validate(payload["task"])
        return OrchestratorState(
            current_step="persist_memory",
            session=session,
            task=task,
            normalized_task_text=task.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="success",
                summary="fake graph completed",
            ),
            progress_updates=["task ingested", "worker result received"],
        ).model_dump(mode="json")


class _RecordingProgressNotifier:
    """Capture progress events emitted by the task execution service."""

    def __init__(self) -> None:
        self.events: list[execution_module.ProgressEvent] = []

    async def notify(
        self,
        *,
        submission: execution_module.TaskSubmission,
        event: execution_module.ProgressEvent,
    ) -> None:
        self.events.append(event)


def test_validate_callback_url_accepts_hostname_with_public_resolution(monkeypatch) -> None:
    """Hostnames that resolve only to public IPs should still be allowed."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        assert port == 443
        assert type == socket.SOCK_STREAM
        assert proto == socket.IPPROTO_TCP
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    assert (
        execution_module._validate_callback_url("https://callbacks.example.com/status")
        == "https://callbacks.example.com/status"
    )


def test_validate_callback_url_rejects_hostname_with_private_resolution(monkeypatch) -> None:
    """Hostname callbacks should be rejected when DNS resolves to a private address."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", port))]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private or local address"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_validate_callback_url_rejects_hostname_with_mixed_public_and_private_resolution(
    monkeypatch,
) -> None:
    """Mixed DNS answers should fail closed when any resolved address is unsafe."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private or local address"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_validate_callback_url_rejects_unresolvable_hostname(monkeypatch) -> None:
    """Unresolvable callback hosts should fail closed."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        raise socket.gaierror("boom")

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="could not be resolved"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_resolve_callback_hostname_times_out_when_resolution_hangs(monkeypatch) -> None:
    """Hostname resolution should fail closed when the resolver does not return promptly."""

    def slow_lookup(host: str, port: int) -> list[tuple]:
        time.sleep(0.05)
        return []

    monkeypatch.setattr(execution_module, "_lookup_callback_hostname_records", slow_lookup)

    with pytest.raises(ValueError, match="resolution timed out"):
        execution_module._resolve_callback_hostname(
            "callbacks.example.com",
            port=443,
            timeout_seconds=0.01,
        )


def test_resolve_callback_hostname_handles_cancelled_future(monkeypatch) -> None:
    """Resolver cancellation should surface as a validation error rather than escape raw."""

    class _CancelledFuture:
        def result(self, timeout: float):
            raise execution_module.FutureCancelledError()

    class _FakeExecutor:
        def submit(self, func, hostname: str, port: int):
            return _CancelledFuture()

    monkeypatch.setattr(execution_module, "_get_callback_dns_executor", lambda: _FakeExecutor())

    with pytest.raises(ValueError, match="resolution was cancelled"):
        execution_module._resolve_callback_hostname("callbacks.example.com", port=443)


def test_resolve_callback_hostname_ignores_non_ip_address_families(monkeypatch) -> None:
    """Only IPv4 and IPv6 `getaddrinfo` answers should be considered callback targets."""

    def fake_lookup(host: str, port: int) -> list[tuple]:
        assert host == "callbacks.example.com"
        assert port == 443
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("ignored", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
        ]

    monkeypatch.setattr(execution_module, "_lookup_callback_hostname_records", fake_lookup)

    assert execution_module._resolve_callback_hostname("callbacks.example.com", port=443) == [
        "93.184.216.34"
    ]


def test_shutdown_callback_dns_executor_recreates_executor_on_next_use() -> None:
    """Executor teardown should not permanently disable later callback resolution."""
    first_executor = execution_module._get_callback_dns_executor()

    execution_module.shutdown_callback_dns_executor()

    second_executor = execution_module._get_callback_dns_executor()

    assert second_executor is not first_executor

    execution_module.shutdown_callback_dns_executor()


def test_is_unsafe_callback_address_rejects_ipv4_mapped_ipv6_loopback() -> None:
    """IPv4-mapped IPv6 addresses should inherit unsafe checks from their IPv4 target."""
    assert execution_module._is_unsafe_callback_address(
        execution_module.ipaddress.ip_address("::ffff:127.0.0.1")
    )


def test_apply_execution_budget_policy_defaults_to_unattended_for_non_telegram_channels() -> None:
    """Non-Telegram channels should receive stricter unattended runtime defaults."""
    budget = execution_module._apply_execution_budget_policy(
        channel="webhook:ci",
        constraints={},
        budget={},
    )

    assert budget["execution_mode"] == "unattended"
    assert budget["max_iterations"] == 5
    assert budget["worker_timeout_seconds"] == 180
    assert budget["max_tool_calls"] == 12
    assert budget["max_shell_commands"] == 12
    assert budget["max_retries"] == 1


def test_apply_execution_budget_policy_defaults_to_interactive_for_telegram() -> None:
    """Telegram channels should receive interactive runtime defaults."""
    budget = execution_module._apply_execution_budget_policy(
        channel="telegram",
        constraints={},
        budget={},
    )

    assert budget["execution_mode"] == "interactive"
    assert budget["max_iterations"] == 8
    assert budget["worker_timeout_seconds"] == 300
    assert budget["max_tool_calls"] == 24
    assert budget["max_shell_commands"] == 24
    assert budget["max_retries"] == 2


def test_apply_execution_budget_policy_treats_channel_case_insensitively() -> None:
    """Channel matching for execution mode should be case-insensitive."""
    budget = execution_module._apply_execution_budget_policy(
        channel="Telegram",
        constraints={},
        budget={},
    )

    assert budget["execution_mode"] == "interactive"


def test_apply_execution_budget_policy_respects_explicit_execution_mode_override() -> None:
    """Explicit mode override should take precedence over channel defaults."""
    budget = execution_module._apply_execution_budget_policy(
        channel="telegram",
        constraints={"execution_mode": "unattended"},
        budget={},
    )
    assert budget["execution_mode"] == "unattended"
    assert budget["worker_timeout_seconds"] == 180


def test_apply_execution_budget_policy_prefers_constraints_over_budget_execution_mode() -> None:
    """Constraints execution_mode should override a conflicting budget execution_mode."""
    budget = execution_module._apply_execution_budget_policy(
        channel="telegram",
        constraints={"execution_mode": "unattended"},
        budget={"execution_mode": "interactive"},
    )

    assert budget["execution_mode"] == "unattended"


def test_apply_execution_budget_policy_invalid_execution_mode_falls_back_to_channel_default() -> (
    None
):
    """Invalid execution_mode values should be ignored and channel defaults should apply."""
    budget = execution_module._apply_execution_budget_policy(
        channel="telegram",
        constraints={"execution_mode": "daemon"},
        budget={"execution_mode": "batch"},
    )

    assert budget["execution_mode"] == "interactive"


def test_apply_execution_budget_policy_preserves_max_minutes_as_timeout_alternative() -> None:
    """Valid max_minutes should prevent worker-timeout defaults from overriding it."""
    budget = execution_module._apply_execution_budget_policy(
        channel="webhook:ci",
        constraints={},
        budget={"max_minutes": 4},
    )

    assert budget["max_minutes"] == 4
    assert "worker_timeout_seconds" not in budget


def test_apply_execution_budget_policy_caps_oversized_runtime_limits() -> None:
    """Oversized budget requests should be clamped to global hard caps."""
    budget = execution_module._apply_execution_budget_policy(
        channel="webhook:ci",
        constraints={},
        budget={
            "max_iterations": 999,
            "worker_timeout_seconds": "9999",
            "max_minutes": 120,
            "orchestrator_timeout_seconds": 4000,
            "max_tool_calls": "500",
            "max_shell_commands": 1000,
            "max_retries": 50,
            "max_verifier_passes": 40,
            "max_observation_characters": 999_999,
        },
    )

    assert budget["max_iterations"] == 20
    assert budget["worker_timeout_seconds"] == 900
    assert budget["max_minutes"] == 15
    assert budget["orchestrator_timeout_seconds"] == 930
    assert budget["max_tool_calls"] == 100
    assert budget["max_shell_commands"] == 100
    assert budget["max_retries"] == 10
    assert budget["max_verifier_passes"] == 5
    assert budget["max_observation_characters"] == 12000


def test_apply_execution_budget_policy_keeps_zero_for_non_negative_limits() -> None:
    """Non-negative budget knobs should preserve explicit zero values."""
    budget = execution_module._apply_execution_budget_policy(
        channel="webhook:ci",
        constraints={},
        budget={
            "max_retries": 0,
            "max_verifier_passes": 0,
            "max_tool_calls": 0,
            "max_shell_commands": 0,
        },
    )

    assert budget["max_retries"] == 0
    assert budget["max_verifier_passes"] == 0
    assert budget["max_tool_calls"] == 0
    assert budget["max_shell_commands"] == 0


def test_apply_execution_budget_policy_drops_invalid_capped_values() -> None:
    """Invalid values for capped budget keys should be removed from effective runtime budget."""
    budget = execution_module._apply_execution_budget_policy(
        channel="webhook:ci",
        constraints={},
        budget={
            "max_minutes": "abc",
            "max_observation_characters": "NaN",
        },
    )

    assert "max_minutes" not in budget
    assert "max_observation_characters" not in budget


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
    assert task_payload["budget"]["worker_timeout_seconds"] == 180
    assert task_payload["budget"]["max_tool_calls"] == 100


def test_run_orchestrator_emits_manual_span_when_otel_available(monkeypatch) -> None:
    """Graph execution should create an explicit OTEL span when tracing is installed."""
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

    started_spans: list[dict[str, object]] = []

    class _FakeSpan:
        def __init__(self, name: str, attributes: dict[str, object] | None) -> None:
            self.name = name
            self.attributes = dict(attributes or {})

        def __enter__(self) -> _FakeSpan:
            started_spans.append({"name": self.name, "attributes": self.attributes})
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    class _FakeTracer:
        def start_as_current_span(
            self, name: str, attributes: dict[str, object] | None = None
        ) -> _FakeSpan:
            return _FakeSpan(name, attributes)

    class _FakeTraceApi:
        def get_tracer(self, name: str) -> _FakeTracer:
            assert name == "orchestrator.execution"
            return _FakeTracer()

    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=_FakeTraceApi()))

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(task_text="Trace graph run")
    _, persisted = service.create_task(submission)

    asyncio.run(service._run_orchestrator(submission, persisted))

    assert started_spans
    graph_run_span = next((s for s in started_spans if s["name"] == "orchestrator.graph.run"), None)
    assert graph_run_span is not None, f"orchestrator.graph.run span not found in {started_spans}"
    attributes = graph_run_span["attributes"]
    assert attributes["openinference.span.kind"] == "CHAIN"
    assert attributes["code_agent.task_id"] == persisted.task_id
    assert attributes["code_agent.session_id"] == persisted.session_id
    assert attributes["code_agent.channel"] == persisted.channel
    assert attributes["code_agent.attempt_count"] == persisted.attempt_count


def test_persist_execution_outcome_span_includes_session_id(monkeypatch) -> None:
    """Execution outcome span should include session correlation attributes."""
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
        task_text="Persist with span correlation",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    captured_span: dict[str, object] = {}

    @contextmanager
    def _fake_start_optional_span(
        *,
        tracer_name: str,
        span_name: str,
        attributes: dict[str, object] | None = None,
    ):
        captured_span["tracer_name"] = tracer_name
        captured_span["span_name"] = span_name
        captured_span["attributes"] = dict(attributes or {})
        yield None

    monkeypatch.setattr(execution_module, "start_optional_span", _fake_start_optional_span)

    state = OrchestratorState(
        current_step="persist_memory",
        attempt_count=3,
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
        result=WorkerResult(status="success", summary="ok"),
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    assert captured_span["tracer_name"] == "orchestrator.execution"
    assert captured_span["span_name"] == "task_execution_service.persist_execution_outcome"
    attributes = captured_span["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["openinference.span.kind"] == "CHAIN"
    assert attributes["code_agent.task_id"] == persisted.task_id
    assert attributes["code_agent.session_id"] == persisted.session_id
    assert attributes["code_agent.channel"] == persisted.channel
    assert attributes["code_agent.attempt_count"] == 3


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


@pytest.mark.anyio
async def test_submit_task_moves_sync_persistence_work_off_thread(monkeypatch) -> None:
    """Async task execution should route sync persistence work through anyio's threadpool."""
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
        task_text="Run the task service",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
        attempt_count=0,
    )

    snapshot = execution_module.TaskSnapshot(
        task_id="task-1",
        session_id="session-1",
        status="completed",
        task_text=submission.task_text,
        repo_url=submission.repo_url,
        branch=submission.branch,
        priority=submission.priority,
        chosen_worker="codex",
        route_reason="cheap_mechanical_change",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    recorded_calls: list[str] = []

    async def fake_run_sync(func):
        recorded_calls.append(func.func.__name__)
        return func()

    def fake_mark_task_in_progress(*, task_id: str) -> None:
        return None

    def fake_persist_execution_outcome(**kwargs) -> None:
        return None

    def fake_get_task(task_id: str) -> execution_module.TaskSnapshot:
        return snapshot

    def fake_log_task_outcome(task_snapshot: execution_module.TaskSnapshot) -> None:
        return None

    monkeypatch.setattr(execution_module.to_thread, "run_sync", fake_run_sync)
    monkeypatch.setattr(service, "_mark_task_in_progress", fake_mark_task_in_progress)
    monkeypatch.setattr(service, "_persist_execution_outcome", fake_persist_execution_outcome)
    monkeypatch.setattr(service, "get_task", fake_get_task)
    monkeypatch.setattr(service, "_log_task_outcome", fake_log_task_outcome)

    await service.submit_task(submission, persisted)

    assert recorded_calls == [
        "fake_mark_task_in_progress",
        "_get_count",
        "fake_persist_execution_outcome",
        "fake_get_task",
    ]


@pytest.mark.anyio
async def test_submit_task_emits_progress_notifications_for_success(monkeypatch) -> None:
    """Successful task execution should emit started, running, and completed updates."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    submission = execution_module.TaskSubmission(task_text="Notify success")
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:100",
        task_id="task-1",
        attempt_count=0,
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        return OrchestratorState(
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
            result=WorkerResult(status="success", summary="all done"),
        )

    completed_snapshot = execution_module.TaskSnapshot(
        task_id=persisted.task_id,
        session_id=persisted.session_id,
        status="completed",
        task_text=submission.task_text,
        priority=submission.priority,
        chosen_worker="codex",
        route_reason="cheap_mechanical_change",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        latest_run=execution_module.WorkerRunSnapshot(
            run_id="run-1",
            worker_type="codex",
            status="success",
            started_at=datetime.now(),
            summary="all done",
        ),
    )

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", lambda *, task_id: None)
    monkeypatch.setattr(service, "_persist_execution_outcome", lambda **kwargs: None)
    monkeypatch.setattr(service, "get_task", lambda task_id: completed_snapshot)
    monkeypatch.setattr(service, "_log_task_outcome", lambda task_snapshot: None)

    await service.submit_task(submission, persisted)

    assert [event.phase for event in notifier.events] == ["started", "running", "completed"]
    assert notifier.events[-1].summary == "all done"


@pytest.mark.anyio
async def test_submit_task_marks_task_failed_when_outcome_persistence_crashes(
    monkeypatch,
) -> None:
    """Persistence failures should not leave the task stuck in progress."""
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
        task_text="Fail after orchestration finishes",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        return OrchestratorState(
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
            result=WorkerResult(status="success", summary="orchestrator finished"),
        )

    def fake_persist_execution_outcome(**kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_persist_execution_outcome", fake_persist_execution_outcome)

    await service.submit_task(submission, persisted)

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.status == TaskStatus.FAILED.value
    assert task_snapshot.latest_run is None


@pytest.mark.anyio
async def test_submit_task_logs_and_exits_when_failed_task_cannot_be_reloaded(
    monkeypatch,
    caplog,
) -> None:
    """The background task should not crash if the failed task snapshot cannot be reloaded."""
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
        task_text="Fail and skip reload",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
        attempt_count=0,
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        raise RuntimeError("orchestrator boom")

    def fake_mark_task_in_progress(*, task_id: str) -> None:
        return None

    def fake_mark_task_failed(*, task_id: str) -> None:
        return None

    def fake_get_task(task_id: str) -> None:
        return None

    def fake_log_task_outcome(task_snapshot: execution_module.TaskSnapshot) -> None:
        raise AssertionError("should not log a missing snapshot")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", fake_mark_task_in_progress)
    monkeypatch.setattr(service, "_mark_task_failed", fake_mark_task_failed)
    monkeypatch.setattr(service, "get_task", fake_get_task)
    monkeypatch.setattr(service, "_log_task_outcome", fake_log_task_outcome)

    with caplog.at_level(logging.ERROR):
        await service.submit_task(submission, persisted)

    assert "Failed to reload task snapshot after marking a background task as failed" in caplog.text


@pytest.mark.anyio
async def test_submit_task_emits_failed_notification_when_snapshot_reload_fails(
    monkeypatch,
) -> None:
    """Failure notifications should still be emitted when the final task snapshot is missing."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    submission = execution_module.TaskSubmission(task_text="Notify failure")
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
        attempt_count=0,
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", lambda *, task_id: None)
    monkeypatch.setattr(service, "_mark_task_failed", lambda *, task_id: None)
    monkeypatch.setattr(service, "get_task", lambda task_id: None)
    monkeypatch.setattr(service, "_log_task_outcome", lambda task_snapshot: None)

    await service.submit_task(submission, persisted)

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]
    assert notifier.events[-1].summary == (
        "Task execution failed and the final snapshot could not be reloaded."
    )


@pytest.mark.anyio
async def test_submit_task_marks_span_error_when_execution_fails(monkeypatch) -> None:
    """Handled submit-task failures should explicitly mark the span as errored."""
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
    submission = execution_module.TaskSubmission(task_text="trace handled error")
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
        attempt_count=0,
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        raise RuntimeError("orchestrator boom")

    span_error_descriptions: list[str | None] = []

    def fake_set_span_error_status(span, *, description: str | None = None) -> None:
        del span
        span_error_descriptions.append(description)

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", lambda *, task_id: None)
    monkeypatch.setattr(service, "_mark_task_failed", lambda *, task_id: None)
    monkeypatch.setattr(service, "get_task", lambda task_id: None)
    monkeypatch.setattr(service, "_log_task_outcome", lambda task_snapshot: None)
    monkeypatch.setattr(execution_module, "set_span_error_status", fake_set_span_error_status)

    await service.submit_task(submission, persisted)

    assert span_error_descriptions == ["RuntimeError: orchestrator boom"]


def test_persist_execution_outcome_creates_error_worker_run_without_result() -> None:
    """Missing worker results should still leave an error worker-run record for observability."""
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
        task_text="Persist an error run",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
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


def test_retention_cleanup_clears_workspace_files_and_persisted_artifacts(
    tmp_path: Path,
) -> None:
    """Expired retained runs should remove both DB artifacts and the workspace on disk."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    workspace_root = tmp_path / "workspaces"
    workspace_path = workspace_root / "workspace-retained"
    artifact_path = workspace_path / "artifacts" / "command-123"
    artifact_path.mkdir(parents=True)
    (artifact_path / "stdout.log").write_text("old stdout\n", encoding="utf-8")

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

        worker_run = worker_run_repo.create(
            task_id=task.id,
            session_id=persisted.session_id,
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
        task_text="Persist interrupted run",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
        current_step="await_approval",
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
        task_text="Persist session state",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
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
        "items": [{"label": "worker_status", "status": "passed", "message": None}],
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
        task_text="Persist review artifact",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
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
        result=WorkerResult(
            status="success",
            summary="done",
            review_result=ReviewResult(
                reviewer_kind="worker_self_review",
                summary="One issue found in changed logic.",
                confidence=0.82,
                outcome="findings",
                findings=[
                    ReviewFinding(
                        severity="medium",
                        category="logic",
                        confidence=0.82,
                        file_path="workers/codex_cli_worker.py",
                        line_start=120,
                        line_end=121,
                        title="Missing empty-result guard",
                        why_it_matters="A missing guard can raise unexpectedly for empty outputs.",
                        evidence="Code path assumes non-empty command output before indexing.",
                        suggested_fix="Guard against empty output before indexing.",
                    )
                ],
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
        task_text="Persist review artifacts",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)
    assert persisted is not None

    state = OrchestratorState(
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
        result=WorkerResult(
            status="success",
            summary="done",
            review_result=ReviewResult(
                reviewer_kind="worker_self_review",
                summary="self review",
                confidence=0.8,
                outcome="findings",
                findings=[
                    ReviewFinding(
                        severity="low",
                        category="tests",
                        confidence=0.8,
                        file_path="tests/unit/test_task_execution_service.py",
                        line_start=1,
                        line_end=1,
                        title="Self review finding",
                        why_it_matters="Ensures review artifacts are persisted.",
                    )
                ],
            ),
        ),
        review=ReviewResult(
            reviewer_kind="independent_reviewer",
            summary="independent review",
            confidence=0.9,
            outcome="findings",
            findings=[
                ReviewFinding(
                    severity="medium",
                    category="correctness",
                    confidence=0.9,
                    file_path="orchestrator/review.py",
                    line_start=1,
                    line_end=1,
                    title="Independent review finding",
                    why_it_matters="Ensures independent review artifacts are persisted.",
                )
            ],
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
        task_text="Persist raw verification mapping",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState.model_construct(
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
        result=WorkerResult(status="success", summary="done"),
        verification={
            "status": "passed",
            "summary": "Verifier accepted the run.",
            "items": [],
        },
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
    assert task_snapshot.latest_run.verifier_outcome == {
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [],
    }


def test_load_submission_for_task_restores_constraints_budget_and_worker_override() -> None:
    """Queued task loading should preserve execution controls from the submitted payload."""
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
            worker_override="gemini",
            constraints={"requires_approval": True, "approval_reason": "manual gate"},
            budget={"max_iterations": 5},
        )
    )

    loaded = service._load_submission_for_task(task_id=task_snapshot.task_id)
    assert loaded is not None
    submission, _ = loaded
    assert submission.worker_override == "gemini"
    assert submission.constraints["requires_approval"] is True
    assert submission.constraints["approval_reason"] == "manual gate"
    assert submission.budget == {"max_iterations": 5}


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

    original_get_user = UserRepository.get_by_external_user_id
    original_get_session = SessionRepository.get_by_channel_thread
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


def test_claim_next_task_allows_single_claim_and_lease_reclaim() -> None:
    """Only one worker should claim a pending task until the lease expires."""
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
    submission = execution_module.TaskSubmission(task_text="claim me")
    snapshot, _ = service.create_task(submission)

    first_claim = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert first_claim is not None
    assert first_claim.task_id == snapshot.task_id
    assert first_claim.attempt_count == 1
    assert service.claim_next_task(worker_id="worker-b", lease_seconds=60) is None

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.lease_expires_at is not None
        task.lease_expires_at = task.lease_expires_at - timedelta(seconds=120)

    reclaimed = service.claim_next_task(worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.task_id == snapshot.task_id
    assert reclaimed.attempt_count == 2


def test_release_failure_requeues_until_max_attempts_then_fails() -> None:
    """Failed attempts should requeue until max attempts, then become terminally failed."""
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
        default_task_max_attempts=2,
    )
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="retry me"))

    claim_one = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert claim_one is not None
    service._release_task_failure(task_id=snapshot.task_id, worker_id="worker-a")

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is not None

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        task.next_attempt_at = utc_now() - timedelta(seconds=1)

    claim_two = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert claim_two is not None
    service._release_task_failure(task_id=snapshot.task_id, worker_id="worker-a")

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED


def test_run_queued_task_requeues_failed_result_when_retries_remain(monkeypatch) -> None:
    """Queued execution should preserve retryability for worker-declared failures."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires retry",
            repo_url="https://github.com/natanayalo/code-agent",
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="run_worker",
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
                task_text=persisted.task_text,
                repo_url=persisted.repo_url,
                branch=persisted.branch,
                priority=persisted.priority,
                worker_override=persisted.worker_override,
                constraints={},
                budget={},
            ),
            normalized_task_text=persisted.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="failure",
                summary="Simulated failure should be retried.",
            ),
        )

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.attempt_count == 1
        assert task.next_attempt_at is not None
        assert task.lease_owner is None
        assert task.lease_expires_at is None


def test_run_queued_task_marks_span_error_when_execution_fails(monkeypatch) -> None:
    """Handled queued-task failures should explicitly mark the span as errored."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="queued span error",
            repo_url="https://github.com/natanayalo/code-agent",
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        raise RuntimeError("queued boom")

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    span_error_descriptions: list[str | None] = []

    def fake_set_span_error_status(span, *, description: str | None = None) -> None:
        del span
        span_error_descriptions.append(description)

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(execution_module, "set_span_error_status", fake_set_span_error_status)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert span_error_descriptions == ["RuntimeError: queued boom"]
    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]


def test_heartbeat_task_lease_uses_configured_duration(monkeypatch) -> None:
    """Lease heartbeat should extend by the worker-configured lease duration."""
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
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="heartbeat me"))
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=30)
    assert claim is not None

    captured: dict[str, int] = {}
    original_heartbeat = TaskRepository.heartbeat_lease

    def recording_heartbeat(
        self: TaskRepository,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        captured["lease_seconds"] = lease_seconds
        return original_heartbeat(
            self,
            task_id=task_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
        )

    monkeypatch.setattr(TaskRepository, "heartbeat_lease", recording_heartbeat)

    assert service._heartbeat_task_lease(
        task_id=snapshot.task_id,
        worker_id="worker-a",
        lease_seconds=123,
    )
    assert captured["lease_seconds"] == 123


def test_heartbeat_interval_seconds_tracks_lease_duration() -> None:
    """Heartbeat interval should scale with lease and stay inside safe bounds."""
    assert execution_module._heartbeat_interval_seconds(lease_seconds=3) == 1.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=30) == 10.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=90) == 10.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=1) == 1.0


def test_run_queued_task_terminal_interrupt_emits_awaiting_approval_without_requeue(
    monkeypatch,
) -> None:
    """Manual-follow-up failures should stay terminal instead of requeueing."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="await_approval",
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
                task_text=submitted.task_text,
                repo_url=submitted.repo_url,
                branch=submitted.branch,
                priority=submitted.priority,
                worker_override=submitted.worker_override,
                constraints={"requires_approval": True},
                budget={},
            ),
            normalized_task_text=submitted.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(
                required=True, status="pending", approval_type="manual_approval"
            ),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="failure",
                summary="Run paused pending manual approval approval.",
                next_action_hint="await_manual_follow_up",
            ),
        )

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == [
        "started",
        "running",
        "awaiting_approval",
    ]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is None
        assert task.lease_owner is None
        assert task.lease_expires_at is None


def test_run_queued_task_rejected_approval_stays_failed(monkeypatch) -> None:
    """Explicit approval rejection should remain terminally failed, not pending."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="await_approval",
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
                task_text=submitted.task_text,
                repo_url=submitted.repo_url,
                branch=submitted.branch,
                priority=submitted.priority,
                worker_override=submitted.worker_override,
                constraints={"requires_approval": True},
                budget={},
            ),
            normalized_task_text=submitted.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(
                required=True, status="rejected", approval_type="manual_approval"
            ),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="failure",
                summary="Task halted because the requested destructive action was not approved.",
                failure_kind="permission_denied",
                next_action_hint="await_manual_follow_up",
            ),
        )

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        assert task.lease_owner is None
        assert task.lease_expires_at is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "rejected"


def test_apply_task_approval_decision_requeues_approved_task(monkeypatch) -> None:
    """Approving a paused task should move it back to pending for queue pickup."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="await_approval",
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
                task_text=submitted.task_text,
                repo_url=submitted.repo_url,
                branch=submitted.branch,
                priority=submitted.priority,
                worker_override=submitted.worker_override,
                constraints={"requires_approval": True},
                budget={},
            ),
            normalized_task_text=submitted.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(
                required=True, status="pending", approval_type="manual_approval"
            ),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="failure",
                summary="Run paused pending manual approval.",
                next_action_hint="await_manual_follow_up",
            ),
        )

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    decision = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert decision.status == "applied"
    assert decision.task_snapshot is not None
    assert decision.task_snapshot.status == TaskStatus.PENDING.value

    duplicate = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert duplicate.status == "already_applied"

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is not None
        assert task.lease_owner is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "approved"
        assert approval.get("approved") is True


def test_apply_task_approval_decision_reject_is_terminal_and_conflict_is_reported(
    monkeypatch,
) -> None:
    """Rejected decisions stay terminal and opposite follow-up decisions are blocked."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="await_approval",
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
                task_text=submitted.task_text,
                repo_url=submitted.repo_url,
                branch=submitted.branch,
                priority=submitted.priority,
                worker_override=submitted.worker_override,
                constraints={"requires_approval": True},
                budget={},
            ),
            normalized_task_text=submitted.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(
                required=True, status="pending", approval_type="manual_approval"
            ),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="failure",
                summary="Run paused pending manual approval.",
                next_action_hint="await_manual_follow_up",
            ),
        )

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    rejected = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=False)
    assert rejected.status == "applied"
    assert rejected.task_snapshot is not None
    assert rejected.task_snapshot.status == TaskStatus.FAILED.value
    assert rejected.task_snapshot.latest_run is not None
    assert "rejected" in (rejected.task_snapshot.latest_run.summary or "").lower()

    duplicate = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=False)
    assert duplicate.status == "already_applied"

    conflict = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert conflict.status == "conflict"

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "rejected"
        assert approval.get("approved") is False


def test_create_task_persists_encryption_metadata() -> None:
    """Verify that TaskExecutionService correctly tags if secrets were encrypted at creation."""
    from cryptography.fernet import Fernet

    from db.models import Task

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

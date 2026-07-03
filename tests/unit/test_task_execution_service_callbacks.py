# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from tests.unit.task_execution_service_support import *  # noqa: F403


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

    monkeypatch.setattr(execution_policy_module.socket, "getaddrinfo", fake_getaddrinfo)

    assert (
        execution_module._validate_callback_url("https://callbacks.example.com/status")
        == "https://callbacks.example.com/status"
    )


def test_validate_callback_url_rejects_hostname_with_private_resolution(monkeypatch) -> None:
    """Hostname callbacks should be rejected when DNS resolves to a private address."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", port))]

    monkeypatch.setattr(execution_policy_module.socket, "getaddrinfo", fake_getaddrinfo)

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

    monkeypatch.setattr(execution_policy_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private or local address"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_validate_callback_url_rejects_unresolvable_hostname(monkeypatch) -> None:
    """Unresolvable callback hosts should fail closed."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        raise socket.gaierror("boom")

    monkeypatch.setattr(execution_policy_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="could not be resolved"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_resolve_callback_hostname_times_out_when_resolution_hangs(monkeypatch) -> None:
    """Hostname resolution should fail closed when the resolver does not return promptly."""

    def slow_lookup(host: str, port: int) -> list[tuple]:
        time.sleep(0.05)
        return []

    monkeypatch.setattr(execution_policy_module, "_lookup_callback_hostname_records", slow_lookup)

    with pytest.raises(ValueError, match="resolution timed out"):
        execution_policy_module._resolve_callback_hostname(
            "callbacks.example.com",
            port=443,
            timeout_seconds=0.01,
        )


def test_resolve_callback_hostname_handles_cancelled_future(monkeypatch) -> None:
    """Resolver cancellation should surface as a validation error rather than escape raw."""

    class _CancelledFuture:
        def result(self, timeout: float):
            raise execution_policy_module.FutureCancelledError()

    class _FakeExecutor:
        def submit(self, func, hostname: str, port: int):
            return _CancelledFuture()

    monkeypatch.setattr(
        execution_policy_module, "_get_callback_dns_executor", lambda: _FakeExecutor()
    )

    with pytest.raises(ValueError, match="resolution was cancelled"):
        execution_policy_module._resolve_callback_hostname("callbacks.example.com", port=443)


def test_resolve_callback_hostname_ignores_non_ip_address_families(monkeypatch) -> None:
    """Only IPv4 and IPv6 `getaddrinfo` answers should be considered callback targets."""

    def fake_lookup(host: str, port: int) -> list[tuple]:
        assert host == "callbacks.example.com"
        assert port == 443
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("ignored", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
        ]

    monkeypatch.setattr(execution_policy_module, "_lookup_callback_hostname_records", fake_lookup)

    assert execution_policy_module._resolve_callback_hostname(
        "callbacks.example.com", port=443
    ) == ["93.184.216.34"]


def test_shutdown_callback_dns_executor_recreates_executor_on_next_use() -> None:
    """Executor teardown should not permanently disable later callback resolution."""
    first_executor = execution_policy_module._get_callback_dns_executor()

    execution_module.shutdown_callback_dns_executor()

    second_executor = execution_policy_module._get_callback_dns_executor()

    assert second_executor is not first_executor

    execution_module.shutdown_callback_dns_executor()


def test_is_unsafe_callback_address_rejects_ipv4_mapped_ipv6_loopback() -> None:
    """IPv4-mapped IPv6 addresses should inherit unsafe checks from their IPv4 target."""
    assert execution_policy_module._is_unsafe_callback_address(
        execution_policy_module.ipaddress.ip_address("::ffff:127.0.0.1")
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
    assert budget["worker_timeout_seconds"] == 300
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
    assert budget["worker_timeout_seconds"] == 600
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
    assert budget["worker_timeout_seconds"] == 300


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


def test_deep_merge_strips_reserved_keys_recursively() -> None:
    """Reserved keys should be removed even when they appear in nested dict/list overrides."""
    target = {"constraints": {"existing": True}, "items": [{"keep": 1}]}
    source = {
        "constraints": {
            "nested": {"allowed": "yes", "approval": {"status": "forbidden"}},
            "approval": {"status": "forbidden"},
        },
        "items": [{"worker_profile_override": "codex-tool-loop", "keep": 2}],
    }

    merged = execution_module._deep_merge(
        target,
        source,
        reserved_keys={"approval", "worker_profile_override"},
    )

    assert merged == {
        "constraints": {"existing": True, "nested": {"allowed": "yes"}},
        "items": [{"keep": 2}],
    }


def test_execution_mapping_helpers_cover_fallback_paths(caplog: pytest.LogCaptureFixture) -> None:
    """Execution helpers should map statuses deterministically and log safe worker fallbacks."""
    unknown_status_state = _build_state(result=WorkerResult(status="failure"))
    unknown_status_state.result = SimpleNamespace(status="mystery")  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING):
        route_fallback = execution_module._worker_type_for_persistence(
            _build_state(result=WorkerResult(status="failure"), dispatch_worker_type=None)
        )
        default_fallback = execution_module._worker_type_for_persistence(
            _build_state(
                result=WorkerResult(status="failure"),
                chosen_worker=None,
                dispatch_worker_type=None,
            )
        )

    assert execution_module._enum_value(None) is None
    assert execution_module._enum_value(TaskStatus.COMPLETED) == "completed"
    assert execution_module._enum_value(123) == "123"
    assert (
        execution_module._task_status_from_result(
            _build_state(approval_required=True, approval_status="pending")
        )
        is TaskStatus.PENDING
    )
    assert execution_module._task_status_from_result(_build_state(result=None)) is TaskStatus.FAILED
    assert (
        execution_module._task_status_from_result(
            _build_state(result=WorkerResult(status="success"))
        )
        is TaskStatus.COMPLETED
    )
    assert (
        execution_module._worker_run_status_from_result(_build_state(result=None))
        is WorkerRunStatus.ERROR
    )
    assert (
        execution_module._worker_run_status_from_result(
            _build_state(result=WorkerResult(status="failure"))
        )
        is WorkerRunStatus.FAILURE
    )
    assert (
        execution_module._worker_run_status_from_result(unknown_status_state)
        is WorkerRunStatus.ERROR
    )
    assert route_fallback is WorkerType.CODEX
    assert default_fallback is WorkerType.CODEX
    assert "route fallback" in caplog.text
    assert "codex default" in caplog.text


def test_interrupt_helpers_normalize_payloads_and_summaries() -> None:
    """Interrupt normalization should handle mapping/object inputs and readable summaries."""

    class _InterruptObject:
        def __init__(self, value):
            self.value = value

    assert execution_module._interrupt_payload_from_object(
        {"value": {"approval_type": "manual"}}
    ) == {"approval_type": "manual"}
    assert execution_module._interrupt_payload_from_object({"reason": "Need approval"}) == {
        "reason": "Need approval"
    }
    assert execution_module._interrupt_payload_from_object(
        _InterruptObject({"approval_type": "permission_escalation"})
    ) == {"approval_type": "permission_escalation"}
    assert (
        execution_module._interrupt_payload_from_object(_InterruptObject("not-a-mapping")) is None
    )

    permission_summary = execution_module._interrupt_summary(
        [
            {
                "approval_type": "permission_escalation",
                "requested_permission": "workspace_write",
                "reason": "Needs elevated access.",
            }
        ]
    )
    typed_summary = execution_module._interrupt_summary([{"approval_type": "manual_review"}])
    fallback_summary = execution_module._interrupt_summary([{}])

    assert "workspace_write" in permission_summary
    assert permission_summary.endswith("Needs elevated access.")
    assert typed_summary == "Run paused pending manual review approval."
    assert fallback_summary == "Run paused pending manual approval."


def test_trace_and_phoenix_helpers_cover_cache_and_fallback_paths(monkeypatch) -> None:
    """Tracing helpers should parse traceparent values and fall back safely on lookup failures."""
    execution_tracing_module._PHOENIX_PROJECT_ID_CACHE = None
    execution_tracing_module._PHOENIX_LAST_FAILURE = 0
    execution_module._clear_tracing_config_cache()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{"data":{"id":"project-123"}}'

    monkeypatch.setattr(
        execution_tracing_module.urllib.request,
        "urlopen",
        lambda url, timeout: _Response(),
    )

    assert execution_module._get_trace_id_from_context(None) is None
    assert (
        execution_module._get_trace_id_from_context({"traceparent": "00-abc123-def456-01"})
        == "abc123"
    )
    assert execution_module._get_trace_id_from_context({"traceparent": "malformed"}) is None
    assert (
        execution_tracing_module._get_project_id("http://phoenix:6006", "code-agent")
        == "project-123"
    )
    assert (
        execution_tracing_module._get_project_id("http://phoenix:6006", "code-agent")
        == "project-123"
    )

    execution_tracing_module._PHOENIX_PROJECT_ID_CACHE = None
    execution_tracing_module._PHOENIX_LAST_FAILURE = time.time()
    assert (
        execution_tracing_module._get_project_id("http://phoenix:6006", "fallback-name")
        == "fallback-name"
    )


def test_snapshot_mapping_helpers_include_working_context_and_memory_metadata() -> None:
    """Snapshot mappers should surface loaded session context and skeptical memory metadata."""
    service, session_factory = _make_task_service()

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="user:snapshots",
            display_name="Snapshot User",
        )
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-snapshots",
        )
        SessionStateRepository(session).upsert(
            session_id=conversation.id,
            active_goal="Harden execution snapshots",
            decisions_made={"path": "unit"},
            identified_risks={"scope": "service"},
            files_touched=["orchestrator/execution.py"],
        )
        personal_memory = PersonalMemoryRepository(session).upsert(
            memory_key="style",
            value={"tone": "concise"},
            source="user_instruction",
            confidence=0.9,
            scope="global",
            requires_verification=False,
        )
        project_memory = ProjectMemoryRepository(session).upsert(
            repo_url="https://github.com/example/repo",
            memory_key="build_cmd",
            value={"cmd": "pytest"},
            source="repo_analysis",
            confidence=0.8,
            scope="repo",
        )

        loaded = SessionRepository(session).list_all(limit=10, offset=0)[0]
        session_snapshot = service._map_session_to_snapshot(loaded)
        personal_snapshot = service._map_personal_memory_to_snapshot(personal_memory)
        project_snapshot = service._map_project_memory_to_snapshot(project_memory)

    unloaded_snapshot = service._map_session_to_snapshot(
        ConversationSession(
            id="session-unloaded",
            user_id=user.id,
            channel="http",
            external_thread_id="thread-unloaded",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )

    assert session_snapshot.working_context is not None
    assert session_snapshot.working_context.active_goal == "Harden execution snapshots"
    assert session_snapshot.working_context.decisions_made == {"path": "unit"}
    assert session_snapshot.working_context.identified_risks == {"scope": "service"}
    assert session_snapshot.working_context.files_touched == ["orchestrator/execution.py"]
    assert unloaded_snapshot.working_context is None
    assert personal_snapshot.source == "user_instruction"
    assert personal_snapshot.value == {"tone": "concise"}
    assert personal_snapshot.requires_verification is False
    assert project_snapshot.repo_url == "https://github.com/example/repo"
    assert project_snapshot.value == {"cmd": "pytest"}
    assert project_snapshot.scope == "repo"

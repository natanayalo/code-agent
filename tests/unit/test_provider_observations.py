"""Bounded, sanitized provider observation tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.task_service_factory import _build_default_worker_profiles
from orchestrator import provider_observations as observations
from orchestrator.provider_diagnostics_types import DiagnosticCheckResult


def service():
    return SimpleNamespace(
        worker=SimpleNamespace(
            available_workers=lambda: {"codex": object(), "shell": object(), "openrouter": object()}
        ),
        enable_worker_profiles=True,
        worker_profiles={
            "codex": {"worker_type": "codex", "runtime_mode": "native_agent"},
            "shell": {"worker_type": "shell", "runtime_mode": "native_agent"},
        },
    )


def test_selects_only_concrete_enabled_providers():
    configured = service()
    assert observations.selected_providers(configured) == {"codex"}
    configured.enable_worker_profiles = False
    assert observations.selected_providers(configured) == set()


def test_excludes_planning_and_review_profiles_until_execution_is_enabled():
    profiles = _build_default_worker_profiles(
        include_gemini=True,
        include_openrouter=False,
        include_codex_legacy_tool_loop=False,
        include_gemini_legacy_tool_loop=False,
    )
    antigravity = {
        name: profile for name, profile in profiles.items() if profile.worker_type == "antigravity"
    }
    configured = service()
    configured.worker = SimpleNamespace(available_workers=lambda: {"antigravity": object()})
    configured.worker_profiles = {
        name: profile
        for name, profile in antigravity.items()
        if profile.runtime_mode in {"planner_only", "reviewer_only"}
    }
    assert observations.selected_providers(configured) == set()
    configured.worker_profiles["executor"] = antigravity["antigravity-native-executor"]
    assert observations.selected_providers(configured) == {"antigravity"}


@pytest.mark.parametrize(
    "runtime_mode,tags,expected",
    [
        ("native_agent", ["planning"], set()),
        ("tool_loop", ["review"], set()),
        ("reviewer_only", ["execution"], set()),
        ("tool_loop", [], {"codex"}),
        ("native_agent", ["execution"], {"codex"}),
    ],
)
def test_profile_execution_eligibility(runtime_mode, tags, expected):
    configured = service()
    configured.worker_profiles = {
        "codex": {"worker_type": "codex", "runtime_mode": runtime_mode, "capability_tags": tags}
    }
    assert observations.selected_providers(configured) == expected


@pytest.mark.asyncio
async def test_observations_are_sanitized_and_failures_unknown(monkeypatch):
    check = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="unready",
        detail="private secret /path",
        verification_scope="local_runtime",
        blocking=True,
        probe_result="error",
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "_operator_context",
        lambda *_: (SimpleNamespace(is_container_execution=True, executor_image=None), True),
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "check_docker_daemon",
        AsyncMock(return_value=check),
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "check_credentials", lambda *_: None
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "check_cli_binary", lambda *_: None
    )
    samples = await observations.observe_providers(service())
    assert samples[0]["outcome"] == "unknown"
    assert "private" not in str(samples) and "/path" not in str(samples)


@pytest.mark.asyncio
async def test_loop_isolates_errors_and_propagates_cancellation(monkeypatch):
    observe = AsyncMock(side_effect=[RuntimeError("secret"), asyncio.CancelledError()])
    monkeypatch.setattr(observations, "observe_providers", observe)
    monkeypatch.setattr(observations.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await observations.observation_loop(service(), "worker", "process")
    assert observe.await_count == 2


def test_identity_is_allowlisted():
    assert observations.alert_identity("provider|not-a-uuid|codex|credentials") is None
    assert (
        observations.alert_identity("provider|00000000-0000-0000-0000-000000000000|codex|secrets")
        is None
    )


@pytest.mark.asyncio
async def test_independent_checks_and_context_failures(monkeypatch):
    context = SimpleNamespace(is_container_execution=True, executor_image="image")
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "_operator_context", lambda *_: (context, True)
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "check_credentials",
        lambda *_: (_ for _ in ()).throw(OSError("private")),
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "check_cli_binary", lambda *_: None
    )
    ready = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="ready",
        detail="ok",
        verification_scope="local_runtime",
        blocking=False,
    )
    missing = ready.model_copy(update={"name": "executor_image", "status": "unready"})
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "check_docker_daemon",
        AsyncMock(return_value=ready),
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "check_executor_image",
        AsyncMock(return_value=missing),
    )
    result = await observations.observe_providers(service())
    assert [(item["check"], item["outcome"]) for item in result] == [
        ("docker_daemon", "ready"),
        ("executor_image", "unready"),
    ]
    context.is_container_execution = False
    assert await observations.observe_providers(service()) == []
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "_operator_context",
        lambda *_: (_ for _ in ()).throw(ValueError()),
    )
    assert await observations.observe_providers(service()) == []


@pytest.mark.asyncio
async def test_loop_saves_and_handles_replaced_worker(monkeypatch):
    observe = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    monkeypatch.setattr(observations, "observe_providers", observe)
    monkeypatch.setattr(observations, "save_observations", lambda *_: False)
    monkeypatch.setattr(observations.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await observations.observation_loop(service(), "worker", "old-process")


def test_rejects_malformed_identity():
    assert observations.alert_identity("wrong") is None
    assert (
        observations.alert_identity("provider|00000000000000000000000000000000|codex|credentials")
        is None
    )


@pytest.mark.asyncio
async def test_cancellation_during_probe_propagates(monkeypatch):
    context = SimpleNamespace(is_container_execution=True, executor_image=None)
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "_operator_context", lambda *_: (context, True)
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "check_credentials", lambda *_: None
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService, "check_cli_binary", lambda *_: None
    )
    monkeypatch.setattr(
        observations.ProviderDiagnosticsService,
        "check_docker_daemon",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await observations.observe_providers(service())

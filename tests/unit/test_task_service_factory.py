"""Unit tests for env-driven task-service bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env
from workers import CodexCliWorker, CodexExecCliRuntimeAdapter, GeminiCliWorker


def _close_outbound_http_clients(outbound_http_clients) -> None:
    async def _close_clients() -> None:
        await asyncio.gather(
            outbound_http_clients.telegram.aclose(),
            outbound_http_clients.webhook.aclose(),
            return_exceptions=True,
        )

    asyncio.run(_close_clients())


def test_build_task_service_from_env_returns_none_when_disabled() -> None:
    """The default app bootstrap should stay inert until explicitly enabled."""
    assert build_task_service_from_env({}) is None


def test_build_task_service_from_env_requires_database_config() -> None:
    """Enabling the task service without DB settings should fail clearly."""
    with pytest.raises(RuntimeError, match="no database configuration was provided"):
        build_task_service_from_env({"CODE_AGENT_ENABLE_TASK_SERVICE": "1"})


def test_build_task_service_from_env_requires_shared_outbound_clients(tmp_path: Path) -> None:
    """Enabled bootstrap should fail clearly when shared outbound clients are missing."""
    database_path = tmp_path / "code-agent.db"

    with pytest.raises(RuntimeError, match="shared outbound HTTP clients"):
        build_task_service_from_env(
            {
                "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
                "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            }
        )


def test_build_task_service_from_env_builds_a_codex_cli_worker(tmp_path: Path) -> None:
    """The configured service should route through the real Codex CLI worker path."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.worker, CodexCliWorker)
        assert isinstance(service.worker.runtime_adapter, CodexExecCliRuntimeAdapter)
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_builds_gemini_worker_when_configured(tmp_path: Path) -> None:
    """When Gemini env vars are set the service should wire a GeminiCliWorker."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CODE_AGENT_GEMINI_CLI_BIN": "/usr/local/bin/gemini",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.gemini_worker, GeminiCliWorker)
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_create_app_uses_env_bootstrap_when_no_task_service_is_injected(
    monkeypatch,
) -> None:
    """App startup should pick up the env-backed task service builder automatically."""
    sentinel = object()
    seen_kwargs: dict[str, object] = {}

    def _build_task_service_from_env(**kwargs):
        seen_kwargs.update(kwargs)
        return sentinel

    monkeypatch.setattr("apps.api.main.build_task_service_from_env", _build_task_service_from_env)

    app = create_app()

    with TestClient(app) as client:
        assert client.app.state.task_service is sentinel
        assert "outbound_http_clients" in seen_kwargs


def test_create_app_with_injected_task_service_skips_outbound_client_bootstrap(
    monkeypatch,
) -> None:
    """Injected task services should not create unused outbound HTTP clients."""
    monkeypatch.setattr(
        "apps.api.main.create_outbound_http_clients",
        lambda: (_ for _ in ()).throw(AssertionError("should not create clients")),
    )

    sentinel = object()
    app = create_app(task_service=sentinel)

    with TestClient(app) as client:
        assert client.app.state.task_service is sentinel
        assert not hasattr(client.app.state, "outbound_http_clients")


def test_create_app_closes_both_clients_when_startup_bootstrap_fails(monkeypatch) -> None:
    """Startup failures should still close both shared outbound clients."""
    close_calls: list[str] = []

    class _FakeClient:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            close_calls.append(self.name)

    outbound_http_clients = SimpleNamespace(
        telegram=_FakeClient("telegram"),
        webhook=_FakeClient("webhook"),
    )
    monkeypatch.setattr(
        "apps.api.main.create_outbound_http_clients",
        lambda: outbound_http_clients,
    )
    monkeypatch.setattr(
        "apps.api.main.build_task_service_from_env",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    app = create_app()

    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass

    assert set(close_calls) == {"telegram", "webhook"}

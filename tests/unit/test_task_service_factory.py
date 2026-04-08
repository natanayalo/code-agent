"""Unit tests for env-driven task-service bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env
from workers import CodexCliWorker, CodexExecCliRuntimeAdapter, GeminiCliWorker


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
        asyncio.run(outbound_http_clients.telegram.aclose())
        asyncio.run(outbound_http_clients.webhook.aclose())


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
        asyncio.run(outbound_http_clients.telegram.aclose())
        asyncio.run(outbound_http_clients.webhook.aclose())


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

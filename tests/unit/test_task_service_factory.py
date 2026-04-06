"""Unit tests for env-driven task-service bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.main import create_app
from apps.api.task_service_factory import build_task_service_from_env
from workers import CodexCliWorker, CodexExecCliRuntimeAdapter, GeminiCliWorker


def test_build_task_service_from_env_returns_none_when_disabled() -> None:
    """The default app bootstrap should stay inert until explicitly enabled."""
    assert build_task_service_from_env({}) is None


def test_build_task_service_from_env_requires_database_config() -> None:
    """Enabling the task service without DB settings should fail clearly."""
    with pytest.raises(RuntimeError, match="no database configuration was provided"):
        build_task_service_from_env({"CODE_AGENT_ENABLE_TASK_SERVICE": "1"})


def test_build_task_service_from_env_builds_a_codex_cli_worker(tmp_path: Path) -> None:
    """The configured service should route through the real Codex CLI worker path."""
    database_path = tmp_path / "code-agent.db"
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        }
    )

    assert service is not None
    assert isinstance(service.worker, CodexCliWorker)
    assert isinstance(service.worker.runtime_adapter, CodexExecCliRuntimeAdapter)


def test_build_task_service_from_env_builds_gemini_worker_when_configured(tmp_path: Path) -> None:
    """When Gemini env vars are set the service should wire a GeminiCliWorker."""
    database_path = tmp_path / "code-agent.db"
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CODE_AGENT_GEMINI_CLI_BIN": "/usr/local/bin/gemini",
        }
    )

    assert service is not None
    assert isinstance(service.gemini_worker, GeminiCliWorker)


def test_create_app_uses_env_bootstrap_when_no_task_service_is_injected(
    monkeypatch,
) -> None:
    """App creation should pick up the env-backed task service builder automatically."""
    sentinel = object()
    monkeypatch.setattr("apps.api.main.build_task_service_from_env", lambda: sentinel)

    app = create_app()

    assert app.state.task_service is sentinel

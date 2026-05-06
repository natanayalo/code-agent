"""Unit tests for env-driven task-service bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import (
    _coerce_positive_int,
    _database_url_from_env,
    build_task_service_from_env,
)
from orchestrator.brain import RuleBasedOrchestratorBrain
from workers import (
    CodexCliWorker,
    CodexExecCliRuntimeAdapter,
    GeminiCliWorker,
    OpenRouterCliWorker,
)


def _close_outbound_http_clients(outbound_http_clients) -> None:
    async def _close_clients() -> None:
        await asyncio.gather(
            outbound_http_clients.telegram.aclose(),
            outbound_http_clients.webhook.aclose(),
        )

    asyncio.run(_close_clients())


def test_build_task_service_from_env_returns_none_when_disabled() -> None:
    """The default app bootstrap should stay inert until explicitly enabled."""
    assert build_task_service_from_env({}) is None


def test_build_task_service_from_env_requires_database_config() -> None:
    """Enabling the task service without DB settings should fail clearly."""
    with pytest.raises(RuntimeError, match="no database configuration was provided"):
        build_task_service_from_env({"CODE_AGENT_ENABLE_TASK_SERVICE": "1"})


def test_database_url_from_env_prefers_explicit_url() -> None:
    """Explicit DATABASE_URL should be used as-is after trimming whitespace."""
    resolved = _database_url_from_env(
        {
            "DATABASE_URL": "  postgresql+psycopg://user:pass@db:5432/code_agent  ",
            "DATABASE_HOST": "ignored",
            "DATABASE_PORT": "5432",
            "POSTGRES_DB": "ignored",
            "POSTGRES_USER": "ignored",
            "POSTGRES_PASSWORD": "ignored",
        }
    )

    assert resolved == "postgresql+psycopg://user:pass@db:5432/code_agent"


def test_database_url_from_env_builds_split_env_url_with_escaping() -> None:
    """Split DB vars should produce an escaped URL when DATABASE_URL is absent."""
    resolved = _database_url_from_env(
        {
            "DATABASE_DRIVER": "postgresql+psycopg",
            "DATABASE_HOST": "localhost",
            "DATABASE_PORT": "5432",
            "POSTGRES_DB": "code agent",
            "POSTGRES_USER": "user+name",
            "POSTGRES_PASSWORD": "p@ss:word",
        }
    )

    assert resolved == "postgresql+psycopg://user%2Bname:p%40ss%3Aword@localhost:5432/code%20agent"


def test_coerce_positive_int_parses_and_falls_back() -> None:
    """Positive-int parser should ignore missing, invalid, and non-positive values."""
    assert _coerce_positive_int(None, default=3) == 3
    assert _coerce_positive_int("  ", default=3) == 3
    assert _coerce_positive_int("abc", default=3) == 3
    assert _coerce_positive_int("0", default=3) == 3
    assert _coerce_positive_int("-4", default=3) == 3
    assert _coerce_positive_int("8", default=3) == 8


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
        assert service.worker.default_runtime_mode == "native_agent"
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_enables_independent_verifier_flag(
    tmp_path: Path,
) -> None:
    """Bootstrap should expose the independent verifier graph toggle when configured."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_INDEPENDENT_VERIFIER_ENABLED": "1",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert service.enable_independent_verifier is True
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_enables_orchestrator_brain_flag(
    tmp_path: Path,
) -> None:
    """Bootstrap should wire the optional orchestrator-brain provider when configured."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_ORCHESTRATOR_BRAIN_ENABLED": "1",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.orchestrator_brain, RuleBasedOrchestratorBrain)
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_enables_profile_routing_with_defaults(
    tmp_path: Path,
) -> None:
    """Profile-aware mode should attach codex-native defaults when explicitly enabled."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_WORKER_PROFILES_ENABLED": "1",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert service.enable_worker_profiles is True
        assert "codex-native-executor" in service.worker_profiles
        assert service.worker_profiles["codex-native-executor"].runtime_mode == "native_agent"
        assert "codex-native-executor-read-only" in service.worker_profiles
        assert (
            service.worker_profiles["codex-native-executor-read-only"].mutation_policy
            == "read_only"
        )
        assert "openrouter-tool-loop-legacy" not in service.worker_profiles
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_respects_runtime_mode_overrides(
    tmp_path: Path,
) -> None:
    """Bootstrap should respect environment-driven runtime mode overrides for profiles."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_WORKER_PROFILES_ENABLED": "true",
            "CODE_AGENT_CODEX_RUNTIME_MODE": "tool_loop",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert "codex-tool-loop-executor" in service.worker_profiles
        assert service.worker_profiles["codex-tool-loop-executor"].runtime_mode == "tool_loop"
        assert "codex-tool-loop-executor-read-only" in service.worker_profiles
        assert service.worker.default_runtime_mode == "tool_loop"
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_applies_sandbox_image_override(tmp_path: Path) -> None:
    """Configured sandbox image should become the default image for worker containers."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CODE_AGENT_SANDBOX_IMAGE": "code-agent-worker",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.worker, CodexCliWorker)
        assert service.worker.container_manager.default_image == "code-agent-worker"
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_uses_default_workspace_root_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap should mirror the workers' default workspace root when unset."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    fallback_root = tmp_path / "default-workspaces"
    monkeypatch.setattr(
        "apps.api.task_service_factory.default_workspace_root",
        lambda: fallback_root,
    )

    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert service.workspace_root == fallback_root.resolve()
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
        assert service.gemini_worker.default_runtime_mode == "native_agent"
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_respects_gemini_runtime_mode_override(
    tmp_path: Path,
) -> None:
    """Gemini runtime mode override should keep rollback to tool_loop available."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_WORKER_PROFILES_ENABLED": "true",
            "CODE_AGENT_GEMINI_CLI_BIN": "/usr/local/bin/gemini",
            "CODE_AGENT_GEMINI_RUNTIME_MODE": "tool_loop",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.gemini_worker, GeminiCliWorker)
        assert service.gemini_worker.default_runtime_mode == "tool_loop"
        assert "gemini-tool-loop-executor" in service.worker_profiles
        assert "gemini-tool-loop-executor-read-only" in service.worker_profiles
        assert "gemini-native-planner" not in service.worker_profiles
        assert "gemini-native-reviewer" not in service.worker_profiles
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_builds_openrouter_worker_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When OpenRouter API key is set the service should wire an OpenRouterCliWorker."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    monkeypatch.setattr(
        "workers.openrouter_adapter.OpenAI",
        lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **__: None))
        ),
    )
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "OPENROUTER_API_KEY": "test-openrouter-key",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.openrouter_worker, OpenRouterCliWorker)
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_openrouter_profile_requires_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OpenRouter worker can be configured, but profile routing should require explicit opt-in."""
    database_path = tmp_path / "code-agent.db"
    monkeypatch.setattr(
        "workers.openrouter_adapter.OpenAI",
        lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **__: None))
        ),
    )
    outbound_http_clients = create_outbound_http_clients()
    service_without_opt_in = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_WORKER_PROFILES_ENABLED": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "OPENROUTER_API_KEY": "test-openrouter-key",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service_without_opt_in is not None
        assert isinstance(service_without_opt_in.openrouter_worker, OpenRouterCliWorker)
        assert "openrouter-tool-loop-legacy" not in service_without_opt_in.worker_profiles
    finally:
        _close_outbound_http_clients(outbound_http_clients)

    outbound_http_clients_opt_in = create_outbound_http_clients()
    service_with_opt_in = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_WORKER_PROFILES_ENABLED": "true",
            "CODE_AGENT_OPENROUTER_ENABLED": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "OPENROUTER_API_KEY": "test-openrouter-key",
        },
        outbound_http_clients=outbound_http_clients_opt_in,
    )

    try:
        assert service_with_opt_in is not None
        assert isinstance(service_with_opt_in.openrouter_worker, OpenRouterCliWorker)
        assert "openrouter-tool-loop-legacy" in service_with_opt_in.worker_profiles
    finally:
        _close_outbound_http_clients(outbound_http_clients_opt_in)


def test_build_task_service_from_env_adds_telegram_progress_notifier_when_token_present(
    tmp_path: Path,
) -> None:
    """Telegram notifier should be included when a bot token is configured."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CODE_AGENT_TELEGRAM_BOT_TOKEN": "bot-token",
            "CODE_AGENT_TELEGRAM_API_BASE_URL": "https://telegram.example.local",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert len(service.progress_notifier.notifiers) == 2
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_build_task_service_from_env_uses_non_sqlite_engine_path(tmp_path: Path) -> None:
    """Split Postgres env should exercise the non-sqlite engine bootstrap path."""
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_HOST": "localhost",
            "DATABASE_PORT": "5432",
            "POSTGRES_DB": "code_agent",
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": "postgres",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.worker, CodexCliWorker)
    finally:
        _close_outbound_http_clients(outbound_http_clients)


def test_create_app_uses_env_bootstrap_when_no_task_service_is_injected(
    monkeypatch,
) -> None:
    """App startup should pick up the env-backed task service builder automatically."""

    class _FakeTaskService:
        async def __aenter__(self) -> _FakeTaskService:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def is_secret_encryption_active(self) -> bool:
            return True

    sentinel = _FakeTaskService()
    seen_kwargs: dict[str, object] = {}

    def _build_task_service_from_env(**kwargs):
        seen_kwargs.update(kwargs)
        return sentinel

    monkeypatch.setattr("apps.api.main.build_task_service_from_env", _build_task_service_from_env)
    monkeypatch.setattr(
        "apps.api.main.build_api_auth_config_from_env",
        lambda: ApiAuthConfig(shared_secret="test-shared-secret"),
    )

    app = create_app()

    with TestClient(app) as client:
        assert client.app.state.task_service is sentinel
        assert "outbound_http_clients" in seen_kwargs


def test_create_app_bootstraps_observability_on_startup(monkeypatch) -> None:
    """API lifespan should invoke tracing bootstrap during startup."""
    tracing_calls: list[str] = []

    class _FakeTaskService:
        async def __aenter__(self) -> _FakeTaskService:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def is_secret_encryption_active(self) -> bool:
            return True

    sentinel = _FakeTaskService()

    monkeypatch.setattr(
        "apps.api.main.configure_tracing_from_env",
        lambda *, service_name: tracing_calls.append(service_name),
    )
    monkeypatch.setattr("apps.api.main.build_task_service_from_env", lambda **_: sentinel)
    monkeypatch.setattr(
        "apps.api.main.build_api_auth_config_from_env",
        lambda: ApiAuthConfig(shared_secret="test-shared-secret"),
    )

    app = create_app()

    with TestClient(app):
        pass

    assert tracing_calls == ["code-agent-api"]


def test_create_app_requires_api_auth_when_env_bootstrap_builds_task_service(
    monkeypatch,
) -> None:
    """Env-bootstrapped task execution should fail closed without an API shared secret."""
    sentinel = AsyncMock()
    sentinel.__aenter__.return_value = sentinel
    monkeypatch.setattr("apps.api.main.build_task_service_from_env", lambda **_: sentinel)
    monkeypatch.setattr("apps.api.main.build_api_auth_config_from_env", lambda: ApiAuthConfig())

    app = create_app()

    with pytest.raises(RuntimeError, match="CODE_AGENT_API_SHARED_SECRET"):
        with TestClient(app):
            pass


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


def test_create_app_shuts_down_callback_dns_executor_on_exit(monkeypatch) -> None:
    """App shutdown should tear down the shared callback DNS executor."""
    shutdown_calls: list[str] = []

    monkeypatch.setattr(
        "apps.api.main.shutdown_callback_dns_executor",
        lambda: shutdown_calls.append("dns"),
    )

    app = create_app(task_service=object())

    with TestClient(app):
        pass

    assert shutdown_calls == ["dns"]


def test_create_app_fails_fast_when_api_runtime_is_disabled(monkeypatch) -> None:
    """App startup should fail when API runtime mode is disabled."""
    monkeypatch.setattr("apps.api.main.should_run_api", lambda: False)

    app = create_app(task_service=object())

    with pytest.raises(RuntimeError, match="API runtime is disabled"):
        with TestClient(app):
            pass


def test_create_app_closes_both_clients_when_startup_bootstrap_fails(monkeypatch) -> None:
    """Startup failures should still close both shared outbound clients."""
    close_calls: list[str] = []
    dns_shutdown_calls: list[str] = []

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
    monkeypatch.setattr(
        "apps.api.main.shutdown_callback_dns_executor",
        lambda: dns_shutdown_calls.append("dns"),
    )

    app = create_app()

    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass

    assert set(close_calls) == {"telegram", "webhook"}
    assert dns_shutdown_calls == ["dns"]


def test_create_app_logs_warning_when_outbound_client_close_fails(monkeypatch) -> None:
    """App shutdown should warn if either shared outbound client fails to close."""
    warning_calls: list[str] = []

    class _FailingClient:
        async def aclose(self) -> None:
            raise RuntimeError("close failure")

    class _OkClient:
        async def aclose(self) -> None:
            return None

    outbound_http_clients = SimpleNamespace(
        telegram=_FailingClient(),
        webhook=_OkClient(),
    )
    monkeypatch.setattr(
        "apps.api.main.create_outbound_http_clients",
        lambda: outbound_http_clients,
    )
    monkeypatch.setattr(
        "apps.api.main.build_task_service_from_env",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "apps.api.main.logger.warning",
        lambda message, **kwargs: warning_calls.append(message),
    )

    app = create_app()

    with TestClient(app):
        pass

    assert warning_calls == ["Failed to close outbound HTTP client during app shutdown"]

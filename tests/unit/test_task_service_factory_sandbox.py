from unittest.mock import MagicMock, patch

from apps.api.task_service_factory import build_task_service_from_env


def test_build_task_service_with_trusted_patterns():
    env = {
        "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
        "DATABASE_URL": "sqlite:///:memory:",
        "CODE_AGENT_CODEX_TRUSTED_REPO_PATTERNS": ".*github\\.com/.*,.*gitlab\\.com/.*",
    }

    mock_clients = MagicMock()

    with (
        patch("apps.api.task_service_factory.CodexCliWorker") as mock_worker_class,
        patch("apps.api.task_service_factory.create_engine_from_url"),
        patch("apps.api.task_service_factory.create_session_factory"),
    ):
        build_task_service_from_env(env, outbound_http_clients=mock_clients)

        # Verify CodexCliWorker was instantiated with the correct patterns
        args, kwargs = mock_worker_class.call_args
        assert kwargs["trusted_repo_patterns"] == [".*github\\.com/.*", ".*gitlab\\.com/.*"]


def test_build_task_service_without_trusted_patterns():
    env = {
        "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
        "DATABASE_URL": "sqlite:///:memory:",
    }

    mock_clients = MagicMock()

    with (
        patch("apps.api.task_service_factory.CodexCliWorker") as mock_worker_class,
        patch("apps.api.task_service_factory.create_engine_from_url"),
        patch("apps.api.task_service_factory.create_session_factory"),
    ):
        build_task_service_from_env(env, outbound_http_clients=mock_clients)

        # Verify CodexCliWorker was instantiated with None for patterns
        args, kwargs = mock_worker_class.call_args
        assert kwargs["trusted_repo_patterns"] is None

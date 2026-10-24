"""Environment wiring for the isolated post-terminal evaluation stack."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apps.api.config import (
    DEFAULT_RETENTION_SECONDS,
    MAX_EVALUATION_RETENTION_SECONDS,
    SystemConfig,
    resolve_retention_seconds,
)
from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env


def test_resolve_retention_seconds_boundaries() -> None:
    """Verify default fallback and evaluation-mode limit enforcement."""
    # Normal mode defaults to 7 days
    assert resolve_retention_seconds({}) == DEFAULT_RETENTION_SECONDS
    assert resolve_retention_seconds({"CODE_AGENT_RETENTION_SECONDS": "1000000"}) == 1000000

    # Evaluation mode defaults to 72 hours (259,200s)
    eval_env = {"CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "1"}
    assert resolve_retention_seconds(eval_env) == MAX_EVALUATION_RETENTION_SECONDS

    # Evaluation mode permits explicit values <= 259,200s
    assert resolve_retention_seconds({**eval_env, "CODE_AGENT_RETENTION_SECONDS": "86400"}) == 86400
    assert (
        resolve_retention_seconds({**eval_env, "CODE_AGENT_RETENTION_SECONDS": "259200"})
        == MAX_EVALUATION_RETENTION_SECONDS
    )

    # Evaluation mode rejects explicit values > 259,200s
    with pytest.raises(ValueError, match="cannot exceed 259200"):
        resolve_retention_seconds({**eval_env, "CODE_AGENT_RETENTION_SECONDS": "259201"})

    with pytest.raises(ValueError, match="cannot exceed 259200"):
        resolve_retention_seconds({**eval_env, "CODE_AGENT_RETENTION_SECONDS": "604800"})


def test_system_config_retention_seconds() -> None:
    """Verify SystemConfig.load_from_env enforces evaluation-mode retention bounds."""
    cfg = SystemConfig.load_from_env({})
    assert cfg.retention_seconds == DEFAULT_RETENTION_SECONDS

    cfg_eval = SystemConfig.load_from_env(
        {"CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "1"}
    )
    assert cfg_eval.retention_seconds == MAX_EVALUATION_RETENTION_SECONDS

    with pytest.raises(ValueError, match="cannot exceed 259200"):
        SystemConfig.load_from_env(
            {
                "CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "1",
                "CODE_AGENT_RETENTION_SECONDS": "300000",
            }
        )


def test_evaluation_mode_env_disables_in_task_verifier_and_enables_mode(tmp_path: Path) -> None:
    """The dedicated evaluation service opts in and turns off its in-task verifier."""
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'evaluation.db'}",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert service.enable_post_terminal_quality_evaluation is True
        assert service.enable_independent_verifier is False
        assert service.retention_seconds == MAX_EVALUATION_RETENTION_SECONDS
    finally:

        async def close_clients() -> None:
            await asyncio.gather(
                outbound_http_clients.telegram.aclose(),
                outbound_http_clients.webhook.aclose(),
            )

        asyncio.run(close_clients())


def test_evaluation_mode_env_custom_and_rejected_retention(tmp_path: Path) -> None:
    """Explicit valid retention is honored; values above 259,200 raise ValueError."""
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "true",
            "CODE_AGENT_RETENTION_SECONDS": "86400",
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'eval_custom.db'}",
        },
        outbound_http_clients=outbound_http_clients,
    )
    try:
        assert service is not None
        assert service.retention_seconds == 86400
    finally:

        async def close_clients() -> None:
            await asyncio.gather(
                outbound_http_clients.telegram.aclose(),
                outbound_http_clients.webhook.aclose(),
            )

        asyncio.run(close_clients())

    with pytest.raises(ValueError, match="cannot exceed 259200"):
        build_task_service_from_env(
            {
                "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
                "CODE_AGENT_ENABLE_POST_TERMINAL_QUALITY_EVALUATION_MODE": "true",
                "CODE_AGENT_RETENTION_SECONDS": "600000",
                "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'eval_err.db'}",
            },
            outbound_http_clients=outbound_http_clients,
        )

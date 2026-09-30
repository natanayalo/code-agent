"""Environment wiring for the isolated post-terminal evaluation stack."""

from __future__ import annotations

import asyncio
from pathlib import Path

from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env


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
    finally:

        async def close_clients() -> None:
            await asyncio.gather(
                outbound_http_clients.telegram.aclose(),
                outbound_http_clients.webhook.aclose(),
            )

        asyncio.run(close_clients())

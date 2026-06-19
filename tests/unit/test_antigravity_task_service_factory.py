"""Unit tests for Antigravity env-backed task-service bootstrap."""

from __future__ import annotations

import asyncio
from pathlib import Path

from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env
from workers import AntigravityCliRuntimeAdapter, GeminiCliWorker


def _close_outbound_http_clients(outbound_http_clients) -> None:
    async def _close_clients() -> None:
        await asyncio.gather(
            outbound_http_clients.telegram.aclose(),
            outbound_http_clients.webhook.aclose(),
        )

    asyncio.run(_close_clients())


def test_build_task_service_from_env_builds_antigravity_worker_when_configured(
    tmp_path: Path,
) -> None:
    """Antigravity env vars should wire the canonical worker lane through agy."""
    database_path = tmp_path / "code-agent.db"
    outbound_http_clients = create_outbound_http_clients()
    service = build_task_service_from_env(
        {
            "CODE_AGENT_ENABLE_TASK_SERVICE": "true",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CODE_AGENT_ANTIGRAVITY_CLI_BIN": "/usr/local/bin/agy",
            "CODE_AGENT_ANTIGRAVITY_MODEL": "gemini-3-pro",
            "CODE_AGENT_ANTIGRAVITY_NATIVE_SANDBOX_ENABLED": "1",
            "CODE_AGENT_ANTIGRAVITY_TOOL_PERMISSION": "strict",
            "CODE_AGENT_ANTIGRAVITY_ARTIFACT_REVIEW_POLICY": "manual",
            "CODE_AGENT_ANTIGRAVITY_AUTH_DIR": "/host/keyring",
        },
        outbound_http_clients=outbound_http_clients,
    )

    try:
        assert service is not None
        assert isinstance(service.gemini_worker, GeminiCliWorker)
        assert isinstance(service.gemini_worker.runtime_adapter, AntigravityCliRuntimeAdapter)
        assert service.gemini_worker.runtime_adapter.executable == "/usr/local/bin/agy"
        assert service.gemini_worker.runtime_adapter.model == "gemini-3-pro"
        assert service.gemini_worker.runtime_adapter.tool_permission == "strict"
        assert service.gemini_worker.runtime_adapter.artifact_review_policy == "manual"
        assert service.gemini_worker.native_sandbox_enabled is True
        assert service.gemini_worker.default_runtime_mode == "native_agent"
    finally:
        _close_outbound_http_clients(outbound_http_clients)

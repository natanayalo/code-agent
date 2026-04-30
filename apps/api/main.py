"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.auth import (
    API_SHARED_SECRET_ENV_VAR,
    ApiAuthConfig,
    build_api_auth_config_from_env,
)
from apps.api.config import SystemConfig
from apps.api.progress import create_outbound_http_clients
from apps.api.routes.auth import router as auth_router
from apps.api.routes.health import router as health_router
from apps.api.routes.knowledge_base import router as knowledge_base_router
from apps.api.routes.metrics import router as metrics_router
from apps.api.routes.sessions import router as sessions_router
from apps.api.routes.system import router as system_router
from apps.api.routes.tasks import router as tasks_router
from apps.api.routes.telegram import router as telegram_router
from apps.api.routes.webhook import router as webhook_router
from apps.api.task_service_factory import build_task_service_from_env
from apps.runtime import RUN_API_ENV_VAR, should_run_api
from orchestrator.execution import TaskExecutionService, shutdown_callback_dns_executor

logger = logging.getLogger(__name__)


def create_app(
    *,
    task_service: TaskExecutionService | None = None,
    auth_config: ApiAuthConfig | None = None,
) -> FastAPI:
    """Create a FastAPI app with optional task-execution dependencies."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not should_run_api():
            raise RuntimeError(
                f"API runtime is disabled for this process. Set {RUN_API_ENV_VAR}=1 to enable it."
            )
        if task_service is None:
            outbound_http_clients = create_outbound_http_clients()
            try:
                app.state.outbound_http_clients = outbound_http_clients
                app.state.api_auth_config = (
                    auth_config if auth_config is not None else build_api_auth_config_from_env()
                )
                app.state.system_config = SystemConfig.load_from_env()

                # Startup validation for dashboard auth
                if (
                    app.state.api_auth_config.shared_secret
                    and not app.state.api_auth_config.allowed_origins
                ):
                    logger.warning(
                        "DASHBOARD AUTH WARNING: CODE_AGENT_ALLOWED_ORIGINS is not set. "
                        "Dashboard login will succeed, but all state-changing actions "
                        "(approval, replay, etc.) "
                        "will fail due to mandatory CSRF protection. "
                        "Set CODE_AGENT_ALLOWED_ORIGINS to the dashboard URL (e.g., http://localhost:3000)."
                    )

                app.state.task_service = build_task_service_from_env(
                    outbound_http_clients=outbound_http_clients
                )
                if (
                    app.state.task_service is not None
                    and app.state.api_auth_config.shared_secret is None
                ):
                    raise RuntimeError(
                        "Task service bootstrap requires "
                        f"{API_SHARED_SECRET_ENV_VAR} to protect /tasks and /webhook."
                    )

                # Verify secret encryption is active (Phase 4 security hardening)
                if (
                    app.state.task_service is not None
                    and not app.state.task_service.is_secret_encryption_active()
                ):
                    logger.critical(
                        "SECURITY WARNING: CODE_AGENT_ENCRYPTION_KEY is not set. "
                        "Task secrets will be stored in PLAIN TEXT. "
                        "To enable encryption, set CODE_AGENT_ENCRYPTION_KEY to a "
                        "Base64-encoded 32-byte key (e.g., via "
                        "'cryptography.fernet.Fernet.generate_key()')."
                    )

                if app.state.task_service is not None:
                    async with app.state.task_service:
                        yield
                else:
                    yield
            finally:
                results = await asyncio.gather(
                    outbound_http_clients.telegram.aclose(),
                    outbound_http_clients.webhook.aclose(),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(
                            "Failed to close outbound HTTP client during app shutdown",
                            exc_info=result,
                        )
                shutdown_callback_dns_executor()
        else:
            try:
                app.state.api_auth_config = auth_config or ApiAuthConfig()
                app.state.system_config = SystemConfig.load_from_env()
                app.state.task_service = task_service
                yield
            finally:
                shutdown_callback_dns_executor()

    app = FastAPI(
        title="code-agent",
        version="0.1.0",
        description="Bootstrap API for the code-agent service.",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(tasks_router)
    app.include_router(sessions_router)
    app.include_router(system_router)
    app.include_router(knowledge_base_router)
    app.include_router(webhook_router)
    app.include_router(telegram_router)
    return app


app = create_app()

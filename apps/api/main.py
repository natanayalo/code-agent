"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

import asyncio  # noqa: E402
import logging  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Any

from fastapi import FastAPI  # noqa: E402

from apps.api.auth import (  # noqa: E402
    API_SHARED_SECRET_ENV_VAR,
    ApiAuthConfig,
    build_api_auth_config_from_env,
)
from apps.api.ci_polling import CIPollingScheduler  # noqa: E402
from apps.api.config import SystemConfig  # noqa: E402
from apps.api.progress import create_outbound_http_clients  # noqa: E402
from apps.api.routes.auth import router as auth_router  # noqa: E402
from apps.api.routes.health import router as health_router  # noqa: E402
from apps.api.routes.knowledge_base import router as knowledge_base_router  # noqa: E402
from apps.api.routes.metrics import router as metrics_router  # noqa: E402
from apps.api.routes.proposals import router as proposals_router  # noqa: E402
from apps.api.routes.sessions import router as sessions_router  # noqa: E402
from apps.api.routes.system import router as system_router  # noqa: E402
from apps.api.routes.tasks import router as tasks_router  # noqa: E402
from apps.api.routes.telegram import router as telegram_router  # noqa: E402
from apps.api.routes.webhook import router as webhook_router  # noqa: E402
from apps.api.scheduler import ScoutScheduler  # noqa: E402
from apps.api.task_service_factory import build_task_service_from_env  # noqa: E402
from apps.observability import configure_tracing_from_env  # noqa: E402
from apps.runtime import RUN_API_ENV_VAR, should_run_api  # noqa: E402
from orchestrator.execution import (  # noqa: E402
    TaskExecutionService,
    bootstrap_phoenix_project_id,
    shutdown_callback_dns_executor,
)

logger = logging.getLogger(__name__)


def _validate_security_config(app: FastAPI) -> None:
    """Validate critical security configurations at startup."""
    # Startup validation for dashboard auth
    if app.state.api_auth_config.shared_secret and not app.state.api_auth_config.allowed_origins:
        logger.warning(
            "DASHBOARD AUTH WARNING: CODE_AGENT_ALLOWED_ORIGINS is not set. "
            "Dashboard login will succeed, but all state-changing actions "
            "(approval, replay, etc.) "
            "will fail due to mandatory CSRF protection. "
            "Set CODE_AGENT_ALLOWED_ORIGINS to the dashboard URL (e.g., http://localhost:3000)."
        )

    if app.state.task_service is not None and app.state.api_auth_config.shared_secret is None:
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


def _build_lifespan(
    task_service: TaskExecutionService | None, auth_config: ApiAuthConfig | None
) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not should_run_api():
            raise RuntimeError(
                f"API runtime is disabled for this process. Set {RUN_API_ENV_VAR}=1 to enable it."
            )

        configure_tracing_from_env(service_name="code-agent-api")
        bootstrap_phoenix_project_id()

        if task_service is None:
            outbound_http_clients = create_outbound_http_clients()
            try:
                app.state.outbound_http_clients = outbound_http_clients
                app.state.api_auth_config = (
                    auth_config if auth_config is not None else build_api_auth_config_from_env()
                )
                app.state.system_config = SystemConfig.load_from_env()

                app.state.task_service = build_task_service_from_env(
                    outbound_http_clients=outbound_http_clients
                )
                _validate_security_config(app)

                if app.state.task_service is not None:
                    async with app.state.task_service:
                        scout_scheduler = ScoutScheduler(
                            task_service=app.state.task_service, config=app.state.system_config
                        )
                        scout_scheduler.start()
                        ci_polling_scheduler = CIPollingScheduler(
                            task_service=app.state.task_service, config=app.state.system_config
                        )
                        ci_polling_scheduler.start()
                        try:
                            yield
                        finally:
                            await scout_scheduler.stop()
                            await ci_polling_scheduler.stop()
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

    return lifespan


def _register_routers(app: FastAPI) -> None:
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(tasks_router)
    app.include_router(proposals_router)
    app.include_router(sessions_router)
    app.include_router(system_router)
    app.include_router(knowledge_base_router)
    app.include_router(webhook_router)
    app.include_router(telegram_router)


def create_app(
    *,
    task_service: TaskExecutionService | None = None,
    auth_config: ApiAuthConfig | None = None,
) -> FastAPI:
    """Create a FastAPI app with optional task-execution dependencies."""
    app = FastAPI(
        title="code-agent",
        version="0.1.0",
        description="Bootstrap API for the code-agent service.",
        lifespan=_build_lifespan(task_service, auth_config),
    )
    _register_routers(app)
    return app


app = create_app()

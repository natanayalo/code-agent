"""Environment-driven task-service bootstrap for the API app."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote

from apps.api.progress import (
    CompositeProgressNotifier,
    OutboundHttpClients,
    TelegramProgressNotifier,
    WebhookCallbackProgressNotifier,
)
from apps.runtime import coerce_positive_int_env as _coerce_positive_int
from orchestrator.execution import ProgressNotifier, TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
from workers import (
    CodexCliWorker,
    CodexExecCliRuntimeAdapter,
    GeminiCliRuntimeAdapter,
    GeminiCliWorker,
)
from workers.gemini_cli_adapter import (
    GEMINI_EXECUTABLE_ENV_VAR,
    GEMINI_MODEL_ENV_VAR,
    GEMINI_TIMEOUT_ENV_VAR,
)

ENABLE_TASK_SERVICE_ENV_VAR: Final[str] = "CODE_AGENT_ENABLE_TASK_SERVICE"
DEFAULT_TASK_MAX_ATTEMPTS_ENV_VAR: Final[str] = "CODE_AGENT_QUEUE_MAX_ATTEMPTS"
DATABASE_URL_ENV_VAR: Final[str] = "DATABASE_URL"
DATABASE_DRIVER_ENV_VAR: Final[str] = "DATABASE_DRIVER"
DATABASE_HOST_ENV_VAR: Final[str] = "DATABASE_HOST"
DATABASE_PORT_ENV_VAR: Final[str] = "DATABASE_PORT"
DATABASE_NAME_ENV_VAR: Final[str] = "POSTGRES_DB"
DATABASE_USER_ENV_VAR: Final[str] = "POSTGRES_USER"
DATABASE_PASSWORD_ENV_VAR: Final[str] = "POSTGRES_PASSWORD"
DEFAULT_DATABASE_DRIVER: Final[str] = "postgresql+psycopg"
TELEGRAM_BOT_TOKEN_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_BOT_TOKEN"
TELEGRAM_API_BASE_URL_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_API_BASE_URL"
CHECKPOINT_DB_PATH_ENV_VAR: Final[str] = "CODE_AGENT_CHECKPOINT_DB_PATH"


def _is_enabled(value: str | None) -> bool:
    """Interpret common truthy environment values."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url_from_env(environ: Mapping[str, str]) -> str | None:
    """Resolve a DB URL from either a full URL or the compose-style split variables."""
    explicit_url = environ.get(DATABASE_URL_ENV_VAR)
    if explicit_url is not None and explicit_url.strip():
        return explicit_url.strip()

    required_parts = {
        DATABASE_HOST_ENV_VAR: environ.get(DATABASE_HOST_ENV_VAR),
        DATABASE_PORT_ENV_VAR: environ.get(DATABASE_PORT_ENV_VAR),
        DATABASE_NAME_ENV_VAR: environ.get(DATABASE_NAME_ENV_VAR),
        DATABASE_USER_ENV_VAR: environ.get(DATABASE_USER_ENV_VAR),
        DATABASE_PASSWORD_ENV_VAR: environ.get(DATABASE_PASSWORD_ENV_VAR),
    }
    if any(value is None or not value.strip() for value in required_parts.values()):
        return None

    driver = environ.get(DATABASE_DRIVER_ENV_VAR, DEFAULT_DATABASE_DRIVER).strip()
    return (
        f"{driver}://{quote(required_parts[DATABASE_USER_ENV_VAR] or '', safe='')}:"
        f"{quote(required_parts[DATABASE_PASSWORD_ENV_VAR] or '', safe='')}"
        f"@{required_parts[DATABASE_HOST_ENV_VAR]}:{required_parts[DATABASE_PORT_ENV_VAR]}"
        f"/{quote(required_parts[DATABASE_NAME_ENV_VAR] or '', safe='')}"
    )


def build_task_service_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    outbound_http_clients: OutboundHttpClients | None = None,
) -> TaskExecutionService | None:
    """Build the real task service when the app is explicitly configured for it."""
    resolved_env = os.environ if environ is None else environ
    if not _is_enabled(resolved_env.get(ENABLE_TASK_SERVICE_ENV_VAR)):
        return None

    database_url = _database_url_from_env(resolved_env)
    if database_url is None:
        raise RuntimeError(
            "Task service bootstrap was enabled, but no database configuration was provided. "
            f"Set {DATABASE_URL_ENV_VAR} or the {DATABASE_HOST_ENV_VAR}/"
            f"{DATABASE_PORT_ENV_VAR}/{DATABASE_NAME_ENV_VAR}/"
            f"{DATABASE_USER_ENV_VAR}/{DATABASE_PASSWORD_ENV_VAR} variables."
        )

    if database_url.startswith("sqlite"):
        engine = create_engine_from_url(
            database_url,
            connect_args={"check_same_thread": False},
        )
    else:
        engine = create_engine_from_url(database_url)
    session_factory = create_session_factory(engine)
    codex_worker = CodexCliWorker(runtime_adapter=CodexExecCliRuntimeAdapter.from_env(resolved_env))
    gemini_worker: GeminiCliWorker | None = None
    if any(
        resolved_env.get(k)
        for k in (GEMINI_EXECUTABLE_ENV_VAR, GEMINI_MODEL_ENV_VAR, GEMINI_TIMEOUT_ENV_VAR)
    ):
        gemini_worker = GeminiCliWorker(
            runtime_adapter=GeminiCliRuntimeAdapter.from_env(resolved_env)
        )
    if outbound_http_clients is None:
        raise RuntimeError(
            "Task service bootstrap requires shared outbound HTTP clients for notifier delivery."
        )
    progress_notifiers: list[ProgressNotifier] = [
        WebhookCallbackProgressNotifier(client=outbound_http_clients.webhook)
    ]
    telegram_bot_token = resolved_env.get(TELEGRAM_BOT_TOKEN_ENV_VAR)
    if telegram_bot_token:
        progress_notifiers.append(
            TelegramProgressNotifier(
                bot_token=telegram_bot_token,
                client=outbound_http_clients.telegram,
                api_base_url=resolved_env.get(
                    TELEGRAM_API_BASE_URL_ENV_VAR,
                    "https://api.telegram.org",
                ),
            )
        )
    return TaskExecutionService(
        session_factory=session_factory,
        worker=codex_worker,
        gemini_worker=gemini_worker,
        progress_notifier=CompositeProgressNotifier(progress_notifiers),
        default_task_max_attempts=_coerce_positive_int(
            resolved_env.get(DEFAULT_TASK_MAX_ATTEMPTS_ENV_VAR),
            default=3,
        ),
        checkpoint_path=resolved_env.get(CHECKPOINT_DB_PATH_ENV_VAR),
    )

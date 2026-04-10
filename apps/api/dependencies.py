"""FastAPI dependency helpers for the API entrypoints."""

from __future__ import annotations

import hmac
from typing import cast

from fastapi import HTTPException, Request, status

from apps.api.auth import (
    API_SHARED_SECRET_HEADER,
    TELEGRAM_WEBHOOK_SECRET_HEADER,
    ApiAuthConfig,
)
from orchestrator.execution import TaskExecutionService


def get_task_service(request: Request) -> TaskExecutionService:
    """Return the configured task service or fail clearly when unavailable."""
    task_service = cast(
        TaskExecutionService | None, getattr(request.app.state, "task_service", None)
    )
    if task_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task execution service is not configured for this app instance.",
        )
    return task_service


def get_api_auth_config(request: Request) -> ApiAuthConfig:
    """Return the configured inbound auth settings for the app instance."""
    return cast(ApiAuthConfig, request.app.state.api_auth_config)


def _ensure_secret_matches(
    *,
    request: Request,
    expected_secret: str,
    header_name: str,
    missing_detail: str,
    invalid_detail: str,
) -> None:
    """Reject requests with missing or invalid shared-secret headers."""
    provided_secret = request.headers.get(header_name)
    if provided_secret is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=missing_detail,
        )
    if not hmac.compare_digest(provided_secret, expected_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=invalid_detail,
        )


def require_api_auth(request: Request) -> None:
    """Require the shared API secret for direct task and generic webhook routes."""
    auth_config = get_api_auth_config(request)
    if auth_config.shared_secret is None:
        if getattr(request.app.state, "task_service", None) is None:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured for this app instance.",
        )

    _ensure_secret_matches(
        request=request,
        expected_secret=auth_config.shared_secret,
        header_name=API_SHARED_SECRET_HEADER,
        missing_detail=f"Missing {API_SHARED_SECRET_HEADER} header.",
        invalid_detail="Invalid API authentication secret.",
    )


def require_telegram_webhook_auth(request: Request) -> None:
    """Require Telegram's secret-token header when that verification is configured."""
    auth_config = get_api_auth_config(request)
    if auth_config.telegram_webhook_secret is None:
        return

    _ensure_secret_matches(
        request=request,
        expected_secret=auth_config.telegram_webhook_secret,
        header_name=TELEGRAM_WEBHOOK_SECRET_HEADER,
        missing_detail=f"Missing {TELEGRAM_WEBHOOK_SECRET_HEADER} header.",
        invalid_detail="Invalid Telegram webhook secret.",
    )

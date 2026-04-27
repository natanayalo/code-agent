"""FastAPI dependency helpers for the API entrypoints."""

from __future__ import annotations

import hmac
import logging
from typing import cast
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from apps.api.auth import (
    API_SHARED_SECRET_HEADER,
    DASHBOARD_COOKIE_NAME,
    TELEGRAM_WEBHOOK_SECRET_HEADER,
    ApiAuthConfig,
    decode_dashboard_token,
)
from orchestrator.execution import TaskExecutionService

logger = logging.getLogger(__name__)


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
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API authentication is not configured for this app instance.",
        )

    _ensure_secret_matches(
        request=request,
        expected_secret=auth_config.shared_secret,
        header_name=API_SHARED_SECRET_HEADER,
        missing_detail=f"Missing {API_SHARED_SECRET_HEADER} header.",
        invalid_detail="Invalid API authentication secret.",
    )


def require_dashboard_user(request: Request) -> None:
    """Require a valid dashboard session cookie."""
    auth_config = get_api_auth_config(request)
    if auth_config.shared_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API authentication is not configured for this app instance.",
        )

    token = request.cookies.get(DASHBOARD_COOKIE_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing session cookie.",
        )

    payload = decode_dashboard_token(token, auth_config.shared_secret)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired session token.",
        )


def _enforce_csrf_protection(request: Request) -> None:
    """Reject requests from untrusted origins when using cookie authentication."""
    auth_config = get_api_auth_config(request)

    # 1. Fail closed if allowed origins are not configured.
    if not auth_config.allowed_origins:
        logger.warning(
            "DASHBOARD AUTH WARNING: CODE_AGENT_ALLOWED_ORIGINS is not set. "
            "Dashboard login will succeed, but all state-changing actions "
            "(approval, replay, etc.) will be blocked."
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF protection: Allowed origins are not configured.",
        )

    # 2. Extract and normalize Origin (preferred) or Referer.
    origin = request.headers.get("Origin")
    if not origin:
        referer = request.headers.get("Referer")
        if referer:
            # Extract scheme://host[:port] from Referer
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"

    if not origin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF protection: Missing Origin or Referer header.",
        )

    # 3. Normalize: trim trailing slash and convert to lowercase for comparison.
    normalized_origin = origin.rstrip("/").lower()

    # 4. Direct match against pre-normalized allowlist.
    if normalized_origin not in auth_config.allowed_origins:
        logger.warning(f"CSRF check failed: Origin '{normalized_origin}' not in allowlist.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"CSRF protection: Origin '{normalized_origin}' is not trusted.",
        )


def require_any_valid_auth(request: Request) -> None:
    """Allow either header-based or cookie-based authentication."""
    auth_config = get_api_auth_config(request)
    if auth_config.shared_secret is None:
        # If task service is active but no secret is set, it's a server misconfiguration.
        if getattr(request.app.state, "task_service", None) is not None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="API authentication is not configured for this app instance.",
            )
        return

    # 1. Try header first (for automation/CLI).
    if API_SHARED_SECRET_HEADER in request.headers:
        # If header is present, it MUST be valid. No fallback if invalid.
        require_api_auth(request)
        return

    # 2. Try cookie (for dashboard).
    token = request.cookies.get(DASHBOARD_COOKIE_NAME)
    if token:
        require_dashboard_user(request)
        # If cookie auth is used, enforce CSRF protection for mutating methods.
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            _enforce_csrf_protection(request)
        return

    # 3. Both missing
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            f"Authentication required: Provide {API_SHARED_SECRET_HEADER} header or session cookie."
        ),
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

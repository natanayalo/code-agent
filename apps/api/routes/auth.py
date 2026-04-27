"""Dashboard authentication routes."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from apps.api.auth import (
    DASHBOARD_COOKIE_NAME,
    JWT_EXPIRY_SECONDS,
    ApiAuthConfig,
    create_dashboard_token,
)
from apps.api.dependencies import get_api_auth_config, require_any_valid_auth

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Payload for login requests."""

    secret: str


class LoginResponse(BaseModel):
    """Payload for successful login."""

    status: str = "ok"
    message: str = "Logged in"


class LogoutResponse(BaseModel):
    """Payload for successful logout."""

    status: str = "ok"


class AuthStatusResponse(BaseModel):
    """Payload for auth status checks."""

    authenticated: bool


@router.post("/login", response_model=LoginResponse)
def login(
    login_req: LoginRequest,
    response: Response,
    request: Request,
    auth_config: ApiAuthConfig = Depends(get_api_auth_config),
) -> LoginResponse:
    """Verify shared secret and set session cookie."""
    if auth_config.shared_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API authentication is not configured for this app instance.",
        )

    if not hmac.compare_digest(login_req.secret, auth_config.shared_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent secret.",
        )

    token = create_dashboard_token(auth_config.shared_secret)

    # Determine secure attribute: explicit env override or HTTPS scheme
    is_secure = auth_config.cookie_secure or request.url.scheme == "https"

    response.set_cookie(
        key=DASHBOARD_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=is_secure,
        path="/",
        max_age=JWT_EXPIRY_SECONDS,
    )

    return LoginResponse()


@router.post(
    "/logout", response_model=LogoutResponse, dependencies=[Depends(require_any_valid_auth)]
)
def logout(response: Response) -> LogoutResponse:
    """Clear the session cookie."""
    response.delete_cookie(
        key=DASHBOARD_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return LogoutResponse()


@router.get("/status", response_model=AuthStatusResponse)
def get_auth_status(
    request: Request,
    auth_config: ApiAuthConfig = Depends(get_api_auth_config),
) -> AuthStatusResponse:
    """Check if the current session is authenticated."""
    try:
        require_any_valid_auth(request)
        return AuthStatusResponse(authenticated=True)
    except HTTPException as exc:
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            return AuthStatusResponse(authenticated=False)
        raise

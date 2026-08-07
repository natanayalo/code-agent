"""Unit tests for apps/api/dependencies.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from apps.api.auth import ApiAuthConfig, create_dashboard_token
from apps.api.dependencies import (
    enforce_csrf_protection,
    get_api_auth_config,
    get_system_config,
    get_task_service,
    require_any_valid_auth,
    require_api_auth,
    require_dashboard_user,
    require_telegram_webhook_auth,
)


def test_get_services_and_configs():
    req = MagicMock()
    req.app.state.task_service = None
    with pytest.raises(HTTPException, match="503"):
        get_task_service(req)

    req.app.state.task_service = "svc"
    assert get_task_service(req) == "svc"

    req.app.state.api_auth_config = "auth"
    assert get_api_auth_config(req) == "auth"

    req.app.state.system_config = "sys"
    assert get_system_config(req) == "sys"


def test_require_api_auth():
    req = MagicMock()
    req.app.state.api_auth_config = ApiAuthConfig(shared_secret=None)
    req.app.state.task_service = None
    require_api_auth(req)  # passes when task_service is None and secret is None

    req.app.state.task_service = "svc"
    with pytest.raises(HTTPException, match="500"):
        require_api_auth(req)

    req.app.state.api_auth_config = ApiAuthConfig(shared_secret="a" * 32)
    req.headers.get.return_value = None
    with pytest.raises(HTTPException, match="Missing X-Webhook-Token header"):
        require_api_auth(req)

    req.headers.get.return_value = "wrong"
    with pytest.raises(HTTPException, match="Invalid API authentication secret"):
        require_api_auth(req)

    req.headers.get.return_value = "a" * 32
    require_api_auth(req)


def test_require_dashboard_user_and_csrf():
    secret = "a" * 32
    req = MagicMock()
    req.app.state.api_auth_config = ApiAuthConfig(
        shared_secret=secret, allowed_origins=["https://dashboard.app"]
    )

    req.cookies.get.return_value = None
    with pytest.raises(HTTPException, match="Missing session cookie"):
        require_dashboard_user(req)

    token = create_dashboard_token(secret)
    req.cookies.get.return_value = token
    require_dashboard_user(req)

    # CSRF tests
    req.headers.get.side_effect = lambda k: None
    with pytest.raises(HTTPException, match="CSRF protection: Missing Origin or Referer header"):
        enforce_csrf_protection(req)

    req.headers.get.side_effect = lambda k: "https://untrusted.com/page" if k == "Referer" else None
    with pytest.raises(
        HTTPException, match="CSRF protection: Origin 'https://untrusted.com' is not trusted"
    ):
        enforce_csrf_protection(req)

    req.headers.get.side_effect = lambda k: "https://dashboard.app" if k == "Origin" else None
    enforce_csrf_protection(req)


def test_require_any_valid_auth():
    secret = "a" * 32
    auth_config = ApiAuthConfig(shared_secret=secret, allowed_origins=["https://dashboard.app"])

    # 1. Missing both
    req1 = MagicMock()
    req1.app.state.api_auth_config = auth_config
    req1.headers = {}
    req1.cookies = {}
    with pytest.raises(HTTPException, match="401"):
        require_any_valid_auth(req1)

    # 2. Header auth
    req2 = MagicMock()
    req2.app.state.api_auth_config = auth_config
    req2.headers = {"X-Webhook-Token": secret}
    req2.cookies = {}
    require_any_valid_auth(req2)

    # 3. Cookie auth
    token = create_dashboard_token(secret)
    req3 = MagicMock()
    req3.app.state.api_auth_config = auth_config
    req3.headers = {"Origin": "https://dashboard.app"}
    req3.cookies = {"agent_session": token}
    req3.method = "POST"
    require_any_valid_auth(req3)


def test_require_telegram_webhook_auth():
    req = MagicMock()
    req.app.state.api_auth_config = ApiAuthConfig(telegram_webhook_secret=None)
    require_telegram_webhook_auth(req)

    req.app.state.api_auth_config = ApiAuthConfig(telegram_webhook_secret="tgsecret")
    req.headers = {}
    with pytest.raises(HTTPException, match="401"):
        require_telegram_webhook_auth(req)

    req.headers = {"X-Telegram-Bot-Api-Secret-Token": "tgsecret"}
    require_telegram_webhook_auth(req)

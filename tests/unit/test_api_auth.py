from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt

from apps.api.auth import (
    ApiAuthConfig,
    build_api_auth_config_from_env,
    create_dashboard_token,
    decode_dashboard_token,
)


def test_build_api_auth_config_from_env_trims_secrets() -> None:
    """Configured secrets should be normalized by stripping whitespace."""
    config = build_api_auth_config_from_env(
        {
            "CODE_AGENT_API_SHARED_SECRET": "  shared-secret  ",
            "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "  telegram-secret  ",
            "CODE_AGENT_ALLOWED_ORIGINS": " http://localhost:3000 , https://agent.local ",
            "CODE_AGENT_COOKIE_SECURE": "1",
            "CODE_AGENT_API_FORCE_HTTPS": "1",
        }
    )

    assert config.shared_secret == "shared-secret"
    assert config.telegram_webhook_secret == "telegram-secret"
    assert config.allowed_origins == ["http://localhost:3000", "https://agent.local"]
    assert config.cookie_secure_override is True
    assert config.force_https is True


def test_build_api_auth_config_from_env_treats_blank_as_missing() -> None:
    """Blank secrets should be treated as unset values."""
    config = build_api_auth_config_from_env(
        {
            "CODE_AGENT_API_SHARED_SECRET": "   ",
            "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "\t",
            "CODE_AGENT_ALLOWED_ORIGINS": "",
            "CODE_AGENT_COOKIE_SECURE": "0",
        }
    )

    assert config.shared_secret is None
    assert config.telegram_webhook_secret is None
    assert config.allowed_origins == []
    assert config.cookie_secure_override is False


def test_is_cookie_secure_logic() -> None:
    """ApiAuthConfig.is_cookie_secure should handle overrides and headers correctly."""
    # 1. Default (no overrides, no request)
    config = ApiAuthConfig()
    assert config.is_cookie_secure() is False

    # 2. Explicit override True
    config = ApiAuthConfig(cookie_secure_override=True)
    assert config.is_cookie_secure() is True

    # 3. Explicit override False
    config = ApiAuthConfig(cookie_secure_override=False)
    assert config.is_cookie_secure() is False

    # 4. force_https override
    config = ApiAuthConfig(force_https=True)
    assert config.is_cookie_secure() is True

    # 5. X-Forwarded-Proto handling
    config = ApiAuthConfig()

    mock_request = MagicMock()
    mock_request.url.scheme = "http"
    mock_request.headers = {"X-Forwarded-Proto": "https"}
    assert config.is_cookie_secure(mock_request) is True

    mock_request.headers = {"X-Forwarded-Proto": "HTTP, HTTPS"}
    assert config.is_cookie_secure(mock_request) is True

    mock_request.headers = {"X-Forwarded-Proto": "http"}
    assert config.is_cookie_secure(mock_request) is False

    # 5b. Header key should be matched case-insensitively
    mock_request.headers = {"x-forwarded-proto": "https"}
    assert config.is_cookie_secure(mock_request) is True

    # 6. Direct scheme check
    mock_request.url.scheme = "https"
    mock_request.headers = {}
    assert config.is_cookie_secure(mock_request) is True

    # 7. Case insensitivity
    mock_request.url.scheme = "http"
    mock_request.headers = {"X-Forwarded-Proto": "hTtPs"}
    assert config.is_cookie_secure(mock_request) is True


def test_dashboard_token_roundtrip() -> None:
    """Tokens should be correctly encoded and decoded with the same secret."""
    secret = "test-secret"
    token = create_dashboard_token(secret)

    payload = decode_dashboard_token(token, secret)
    assert payload is not None
    assert payload["sub"] == "operator"
    assert "iat" in payload
    assert "exp" in payload


def test_dashboard_token_fails_with_wrong_secret() -> None:
    """Decoding with the wrong secret should return None."""
    token = create_dashboard_token("right-secret")
    assert decode_dashboard_token(token, "wrong-secret") is None


def test_dashboard_token_expiry() -> None:
    """Expired tokens should return None."""
    # We can't easily inject time into create_dashboard_token without mocking,
    # but we can verify it fails if exp is in the past.
    secret = "test-secret"
    payload = {
        "iat": int(time.time()) - 3601,
        "exp": int(time.time()) - 1,
        "sub": "operator",
    }
    expired_token = jwt.encode(payload, secret, algorithm="HS256")

    assert decode_dashboard_token(expired_token, secret) is None

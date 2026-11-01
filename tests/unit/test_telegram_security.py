"""Configuration/auth/body boundary regressions; no real Telegram credentials."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api.auth import ApiAuthConfig, build_api_auth_config_from_env
from apps.api.dependencies import require_telegram_webhook_auth
from apps.api.telegram_body import TelegramBodyLimitMiddleware
from apps.api.telegram_security import TELEGRAM_BODY_LIMIT, parse_telegram_ids


def operator_config() -> ApiAuthConfig:
    return ApiAuthConfig(
        telegram_webhook_enabled=True,
        telegram_webhook_secret="t" * 32,
        telegram_allowed_user_ids=frozenset({42}),
        telegram_allowed_chat_ids=frozenset({100}),
    )


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_disabled_by_default_and_explicitly(value):
    assert not build_api_auth_config_from_env({}).telegram_webhook_enabled
    assert not build_api_auth_config_from_env(
        {"CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": value}
    ).telegram_webhook_enabled


@pytest.mark.parametrize("secret", [None, "", "a" * 31, "a" * 257, "x" * 31 + "é", "x" * 31 + " "])
def test_enabled_requires_strong_well_formed_secret(secret):
    with pytest.raises(ValueError, match="URL-safe secret"):
        replace(operator_config(), telegram_webhook_secret=secret)


@pytest.mark.parametrize("field", ["telegram_allowed_user_ids", "telegram_allowed_chat_ids"])
@pytest.mark.parametrize("ids", [frozenset(), frozenset({-1}), frozenset({True})])
def test_enabled_requires_allowlists(field, ids):
    with pytest.raises(ValueError, match="allowlists"):
        replace(operator_config(), **{field: ids})


@pytest.mark.parametrize("value", ["-1", "0", "@name", "1,,2", "1,", "True", "１", "1.0"])
def test_ids_are_strict_positive_ascii_numbers(value):
    with pytest.raises(ValueError, match="numeric IDs"):
        parse_telegram_ids(value, setting="allowlist")


def test_environment_configuration_and_invalid_enablement():
    config = build_api_auth_config_from_env(
        {
            "CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": "TRUE",
            "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "t" * 32,
            "CODE_AGENT_GITHUB_WEBHOOK_SECRET": "github-secret",
            "CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS": "42, 42",
            "CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS": "100,200",
        }
    )
    assert config.telegram_allowed_user_ids == frozenset({42})
    assert config.telegram_allowed_chat_ids == frozenset({100, 200})
    assert config.github_webhook_secret == "github-secret"
    with pytest.raises(ValueError, match="boolean"):
        build_api_auth_config_from_env({"CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED": "treu"})


def request(headers=(), config=None):
    return Request(
        {
            "type": "http",
            "path": "/telegram/webhook",
            "raw_path": b"/telegram/webhook",
            "method": "POST",
            "headers": headers,
            "query_string": b"",
            "app": SimpleNamespace(
                state=SimpleNamespace(api_auth_config=config or operator_config())
            ),
        }
    )


@pytest.mark.parametrize(
    "values,code",
    [
        ([], 401),
        ([b"x" * 32], 403),
        ([b"t" * 32, b"t" * 32], 403),
        ([b"t" * 31], 403),
        ([b"\xff" * 32], 403),
        ([b"t" * 32 + b",x"], 403),
    ],
)
def test_secret_rejections(values, code):
    req = request([(b"x-telegram-bot-api-secret-token", value) for value in values])
    with pytest.raises(HTTPException) as exc:
        require_telegram_webhook_auth(req)
    assert exc.value.status_code == code


def test_secret_success_disabled_and_missing_internal_configuration():
    req = request([(b"x-telegram-bot-api-secret-token", b"t" * 32)])
    require_telegram_webhook_auth(req)
    with pytest.raises(HTTPException) as exc:
        require_telegram_webhook_auth(request(config=ApiAuthConfig()))
    assert exc.value.status_code == 404
    req.app.state.api_auth_config = SimpleNamespace(
        telegram_webhook_enabled=True, telegram_webhook_secret=None
    )
    with pytest.raises(HTTPException) as exc:
        require_telegram_webhook_auth(req)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_chunked_body_is_bounded_before_downstream(oversized):
    calls, sent = [], []
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * TELEGRAM_BODY_LIMIT, "more_body": True},
            {"type": "http.request", "body": b"x" if oversized else b"", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        calls.append(await receive())

    scope = request([(b"x-telegram-bot-api-secret-token", b"t" * 32)]).scope
    await TelegramBodyLimitMiddleware(downstream)(scope, receive, send)
    if oversized:
        assert not calls
        assert sent[0]["status"] == 413
    else:
        assert len(calls[0]["body"]) == TELEGRAM_BODY_LIMIT


@pytest.mark.asyncio
async def test_client_disconnect_does_not_dispatch():
    calls = []

    async def downstream(*args):
        calls.append(args)

    async def receive():
        return {"type": "http.disconnect"}

    scope = request([(b"x-telegram-bot-api-secret-token", b"t" * 32)]).scope
    await TelegramBodyLimitMiddleware(downstream)(scope, receive, downstream)
    assert not calls


@pytest.mark.asyncio
async def test_middleware_passthrough_and_auth_rejection_before_reading():
    calls, sent = [], []

    async def downstream(scope, receive, send):
        calls.append(scope["type"])

    async def receive():
        raise AssertionError("disabled or noncanonical ingress must not consume input")

    async def send(message):
        sent.append(message)

    middleware = TelegramBodyLimitMiddleware(downstream)
    await middleware({"type": "websocket"}, receive, send)
    assert calls == ["websocket"]
    for raw, query, config in [
        (b"/telegram/webhook", b"", ApiAuthConfig()),
        (b"/telegram/%77ebhook", b"", operator_config()),
        (b"/telegram/webhook", b"x=1", operator_config()),
    ]:
        scope = request(config=config).scope
        scope.update(raw_path=raw, query_string=query)
        await middleware(scope, receive, send)
    assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [
        404,
        404,
        404,
    ]

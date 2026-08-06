"""Unit tests for telegram, webhook, and auth API routes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from apps.api.auth import ApiAuthConfig
from apps.api.config import SystemConfig
from apps.api.routes.auth import (
    LoginRequest,
    get_auth_status,
    login,
    logout,
)
from apps.api.routes.telegram import (
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
    receive_telegram_update,
)
from apps.api.routes.webhook import (
    WebhookPayload,
    receive_webhook,
)
from orchestrator.execution import TemporalUnavailableError


def test_telegram_webhook_route():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img", workspace_root="/tmp", telegram_default_repo_key="main"
    )

    # Update with no message
    up1 = TelegramUpdate(update_id=1)
    res1 = receive_telegram_update(up1, None, task_service, config)
    assert res1.ok is True and res1.detail == "no_message"

    # Message with no text
    up2 = TelegramUpdate(
        update_id=2, message=TelegramMessage(message_id=10, chat=TelegramChat(id=100))
    )
    res2 = receive_telegram_update(up2, None, task_service, config)
    assert res2.ok is True and res2.detail == "no_text"

    # Message with text > 10000 chars
    long_text = "a" * 10001
    up3 = TelegramUpdate(
        update_id=3,
        message=TelegramMessage(message_id=11, chat=TelegramChat(id=100), text=long_text),
    )
    res3 = receive_telegram_update(up3, None, task_service, config)
    assert res3.ok is True and res3.detail == "text_too_long"

    # Normal message
    up4 = TelegramUpdate(
        update_id=4,
        message=TelegramMessage(
            message_id=12,
            chat=TelegramChat(id=100),
            from_=TelegramUser(id=200, first_name="John", last_name="Doe"),
            text="Hello bot",
        ),
    )
    outcome = MagicMock()
    outcome.duplicate = False
    outcome.task_snapshot.task_id = "t1"
    outcome.task_snapshot.session_id = "s1"
    task_service.create_task_outcome.return_value = outcome

    res4 = receive_telegram_update(up4, None, task_service, config)
    assert res4.ok is True and res4.task_id == "t1"

    # Channel post (no from_)
    up5 = TelegramUpdate(
        update_id=5,
        channel_post=TelegramMessage(
            message_id=13, chat=TelegramChat(id=101), text="Channel announcement"
        ),
    )
    res5 = receive_telegram_update(up5, None, task_service, config)
    assert res5.ok is True

    # Temporal error
    task_service.create_task_outcome.side_effect = TemporalUnavailableError("Down")
    with pytest.raises(HTTPException) as exc:
        receive_telegram_update(up4, None, task_service, config)
    assert exc.value.status_code == 503


def test_webhook_route():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        allowed_repos={"myrepo": "https://github.com/org/repo"},
    )

    # Unknown repo_key
    payload_unknown = WebhookPayload(task_text="Build app", repo_key="unknown_repo")
    with pytest.raises(HTTPException) as exc1:
        receive_webhook(payload_unknown, task_service, config)
    assert exc1.value.status_code == 400

    # Valid payload
    payload_ok = WebhookPayload(task_text="Build app", repo_key="myrepo", external_user_id="u1")
    outcome = MagicMock()
    outcome.duplicate = False
    outcome.task_snapshot.task_id = "t1"
    outcome.task_snapshot.session_id = "s1"
    outcome.task_snapshot.status.value = "pending"
    outcome.task_snapshot.priority = 10
    task_service.create_task_outcome.return_value = outcome

    res = receive_webhook(payload_ok, task_service, config)
    assert res.task_id == "t1"

    # Validation error
    from orchestrator.execution import TaskSubmissionValidationError

    task_service.create_task_outcome.side_effect = TaskSubmissionValidationError("Invalid repo")
    payload_val = WebhookPayload(task_text="Build app")
    with pytest.raises(HTTPException) as exc2:
        receive_webhook(payload_val, task_service, config)
    assert exc2.value.status_code == 422


def test_auth_routes(monkeypatch):
    auth_config = ApiAuthConfig(shared_secret="a" * 32)  # gitleaks:allow
    req = MagicMock(spec=Request)
    req.headers = {}
    req.cookies = {}
    resp = Response()

    # Login missing secret in config
    no_sec_config = ApiAuthConfig(shared_secret=None)
    with pytest.raises(HTTPException) as exc_no_sec:
        login(LoginRequest(secret="sec"), resp, req, no_sec_config)
    assert exc_no_sec.value.status_code == 500

    # Invalid secret
    with pytest.raises(HTTPException) as exc_inv:
        login(LoginRequest(secret="wrong"), resp, req, auth_config)
    assert exc_inv.value.status_code == 401

    # Valid secret
    log_res = login(LoginRequest(secret="a" * 32), resp, req, auth_config)
    assert log_res.status == "ok"

    # Logout
    logout_res = logout(resp, req, auth_config)
    assert logout_res.status == "ok"

    # Auth status success
    monkeypatch.setattr("apps.api.routes.auth.require_any_valid_auth", lambda r: None)
    status_ok = get_auth_status(req, auth_config)
    assert status_ok.authenticated is True

    # Auth status failure
    def _fail(r):
        raise HTTPException(status_code=401, detail="Unauthorized")

    monkeypatch.setattr("apps.api.routes.auth.require_any_valid_auth", _fail)
    status_unauth = get_auth_status(req, auth_config)
    assert status_unauth.authenticated is False

"""Reject oversized signed ingress before untrusted body buffering."""

import hashlib
import hmac
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.api.auth import ApiAuthConfig
from apps.api.dependencies import require_github_webhook_auth

LIMIT = 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("advertised", [None, "1", str(LIMIT + 1)])
async def test_oversized_github_body_stops_reading(advertised):
    reads = []

    async def stream():
        for chunk in [b"x" * LIMIT, b"x", b"must-not-read"]:
            reads.append(len(chunk))
            yield chunk

    async def forbidden_body():
        pytest.fail("unbounded Request.body() must never be called")

    headers = {"X-Hub-Signature-256": "sha256=synthetic"}
    if advertised is not None:
        headers["content-length"] = advertised
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(api_auth_config=ApiAuthConfig(github_webhook_secret="synthetic"))
        ),
        headers=headers,
        stream=stream,
        body=forbidden_body,
    )
    with pytest.raises(HTTPException) as exc:
        await require_github_webhook_auth(request)
    assert exc.value.status_code == 413
    assert len(reads) <= 2


@pytest.mark.asyncio
async def test_exact_limit_preserves_signed_bytes():
    body = b"x" * LIMIT

    async def stream():
        yield body[:15]
        yield body[15:]

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(api_auth_config=ApiAuthConfig(github_webhook_secret="synthetic"))
        ),
        headers={
            "X-Hub-Signature-256": "sha256="
            + hmac.new(b"synthetic", body, hashlib.sha256).hexdigest()
        },
        stream=stream,
    )
    assert await require_github_webhook_auth(request) == body

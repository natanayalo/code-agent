"""Bound Telegram input before JSON decoding, including chunked ASGI requests."""

from __future__ import annotations

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.dependencies import require_telegram_webhook_auth
from apps.api.telegram_security import TELEGRAM_BODY_LIMIT


class TelegramBodyLimitMiddleware:
    """Buffer at most the permitted body; oversized input never reaches the API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/telegram/webhook":
            await self.app(scope, receive, send)
            return
        if scope.get("raw_path", b"/telegram/webhook") != b"/telegram/webhook" or scope.get(
            "query_string"
        ):
            await JSONResponse({"detail": "Not found"}, status_code=404)(scope, receive, send)
            return
        try:
            require_telegram_webhook_auth(Request(scope))
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > TELEGRAM_BODY_LIMIT:
                await JSONResponse({"detail": "Telegram update too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        async def bounded_receive() -> Message:
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)

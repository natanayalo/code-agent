"""Bound GitHub webhook bytes before signature verification and JSON decoding."""

import logging

from fastapi import HTTPException, Request

GITHUB_BODY_LIMIT = 1024 * 1024
logger = logging.getLogger(__name__)


async def read_github_body(request: Request) -> bytes:
    """Count streamed bytes even when Content-Length is absent or inaccurate."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > GITHUB_BODY_LIMIT:
            logger.warning("GitHub webhook rejected: request exceeds body limit")
            raise HTTPException(status_code=413, detail="GitHub webhook payload too large")
        body.extend(chunk)
    return bytes(body)

"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.routes.health import router as health_router

app = FastAPI(
    title="code-agent",
    version="0.1.0",
    description="Bootstrap API for the code-agent service.",
)

app.include_router(health_router)

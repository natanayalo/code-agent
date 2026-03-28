"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="code-agent",
    version="0.1.0",
    description="Bootstrap API for the code-agent service.",
)

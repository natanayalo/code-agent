"""Bootstrap integration tests."""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.main import app


def test_app_bootstrap_imports_cleanly() -> None:
    """The bootstrap slice exposes an importable FastAPI app."""
    assert isinstance(app, FastAPI)
    assert app.title == "code-agent"

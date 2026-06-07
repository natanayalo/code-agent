"""Tracing helpers for execution-path orchestration."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from threading import Lock
from typing import Final
from urllib.parse import urlparse

from apps.observability import (
    is_tracing_enabled,
    resolve_otel_tracing_endpoint,
    resolve_tracing_project_name,
)

logger = logging.getLogger(__name__)

PHOENIX_API_TIMEOUT: Final[float] = 2.0
_PHOENIX_PROJECT_ID_CACHE: str | None = None
_PHOENIX_LAST_FAILURE: float = 0
_PHOENIX_FAILURE_TTL: Final[float] = 60.0
_PHOENIX_PROJECT_ID_LOCK = Lock()

_TRACING_CONFIG_CACHE: tuple[bool, str | None, str] | None = None
_TRACING_CONFIG_LOCK = Lock()


def _get_project_id(api_base_url: str, project_name: str) -> str:
    """Resolve the Phoenix project ID (UUID) from its name via the REST API."""
    global _PHOENIX_PROJECT_ID_CACHE, _PHOENIX_LAST_FAILURE
    if _PHOENIX_PROJECT_ID_CACHE:
        return _PHOENIX_PROJECT_ID_CACHE

    if time.time() - _PHOENIX_LAST_FAILURE < _PHOENIX_FAILURE_TTL:
        return project_name

    with _PHOENIX_PROJECT_ID_LOCK:
        if _PHOENIX_PROJECT_ID_CACHE:
            return _PHOENIX_PROJECT_ID_CACHE
        if time.time() - _PHOENIX_LAST_FAILURE < _PHOENIX_FAILURE_TTL:
            return project_name

        try:
            url = f"{api_base_url}/v1/projects/{urllib.parse.quote(project_name)}"
            with urllib.request.urlopen(url, timeout=PHOENIX_API_TIMEOUT) as response:
                data = json.loads(response.read().decode())
                _PHOENIX_PROJECT_ID_CACHE = data["data"]["id"]
        except (urllib.error.URLError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            logger.debug("Failed to resolve Phoenix project ID for '%s': %s", project_name, exc)
            _PHOENIX_LAST_FAILURE = time.time()
            return project_name

        return _PHOENIX_PROJECT_ID_CACHE or project_name


def _get_tracing_config() -> tuple[bool, str | None, str]:
    """Helper to fetch tracing config once per process."""
    global _TRACING_CONFIG_CACHE
    if _TRACING_CONFIG_CACHE is not None:
        return _TRACING_CONFIG_CACHE

    with _TRACING_CONFIG_LOCK:
        if _TRACING_CONFIG_CACHE is not None:
            return _TRACING_CONFIG_CACHE

        enabled = is_tracing_enabled()
        collector_endpoint = resolve_otel_tracing_endpoint(os.environ)
        project_name = resolve_tracing_project_name(os.environ)
        _TRACING_CONFIG_CACHE = (enabled, collector_endpoint, project_name)
        return _TRACING_CONFIG_CACHE


def _clear_tracing_config_cache() -> None:
    """Internal helper for tests to reset configuration state."""
    global _TRACING_CONFIG_CACHE
    with _TRACING_CONFIG_LOCK:
        _TRACING_CONFIG_CACHE = None


def bootstrap_phoenix_project_id() -> None:
    """Pre-resolve the Phoenix project ID to avoid blocking API threads later."""
    enabled, endpoint, project_name = _get_tracing_config()
    if not enabled or not endpoint:
        return

    api_base_url = endpoint.removesuffix("/v1/traces")
    _get_project_id(api_base_url, project_name)


def _get_phoenix_url(trace_id: str | None) -> str | None:
    """Generate a browser-accessible Phoenix deep link for a given trace ID."""
    if not trace_id:
        return None

    enable_tracing, collector_endpoint, project_name = _get_tracing_config()
    if not enable_tracing:
        return None

    if not collector_endpoint:
        return f"http://localhost:6006/projects/{project_name}/traces/{trace_id}"

    try:
        parsed = urlparse(collector_endpoint)
        api_base_url = f"{parsed.scheme}://{parsed.netloc}"
        project_id = _get_project_id(api_base_url, project_name)

        netloc = parsed.netloc
        if netloc.startswith("phoenix:"):
            netloc = netloc.replace("phoenix:", "localhost:", 1)
        elif netloc == "phoenix":
            netloc = "localhost:6006"

        return f"{parsed.scheme}://{netloc}/projects/{project_id}/traces/{trace_id}"
    except (ValueError, urllib.error.URLError) as exc:
        logger.debug("Failed to generate custom Phoenix deep link: %s", exc)
        return f"http://localhost:6006/projects/{project_name}/traces/{trace_id}"

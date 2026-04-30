"""Startup helpers for opt-in LangSmith OpenTelemetry tracing."""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Callable, MutableMapping
from typing import Final

LANGSMITH_OTEL_ENABLED_ENV_VAR: Final[str] = "LANGSMITH_OTEL_ENABLED"
LANGSMITH_TRACING_ENV_VAR: Final[str] = "LANGSMITH_TRACING"
LANGSMITH_API_KEY_ENV_VAR: Final[str] = "LANGSMITH_API_KEY"
LANGSMITH_PROJECT_ENV_VAR: Final[str] = "LANGSMITH_PROJECT"
LANGSMITH_ENDPOINT_ENV_VAR: Final[str] = "LANGSMITH_ENDPOINT"

_REQUIRED_OTEL_MODULES: Final[tuple[str, ...]] = (
    "opentelemetry.sdk.trace",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
)


def _is_enabled(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _module_exists(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def bootstrap_langsmith_otel(
    *,
    runtime_name: str,
    logger: logging.Logger,
    environ: MutableMapping[str, str] | None = None,
    module_exists: Callable[[str], bool] = _module_exists,
) -> bool:
    """Validate and prepare opt-in LangSmith OTEL tracing settings.

    Returns True when OTEL tracing is enabled and all local prerequisites are present.
    """
    env = os.environ if environ is None else environ
    if not _is_enabled(env.get(LANGSMITH_OTEL_ENABLED_ENV_VAR), default=False):
        return False

    tracing_value = env.get(LANGSMITH_TRACING_ENV_VAR, "").strip()
    if tracing_value == "":
        env[LANGSMITH_TRACING_ENV_VAR] = "true"
        tracing_value = "true"
        # LangSmith caches env lookups in-process; clear cache if it is already imported.
        try:
            from langsmith import utils as langsmith_utils

            cache_clear = getattr(langsmith_utils.get_env_var, "cache_clear", None)
            if callable(cache_clear):
                cache_clear()
        except (ImportError, AttributeError):
            pass
        logger.info(
            "Enabled %s=true because %s is true.",
            LANGSMITH_TRACING_ENV_VAR,
            LANGSMITH_OTEL_ENABLED_ENV_VAR,
            extra={"runtime_name": runtime_name},
        )

    if not _is_enabled(tracing_value, default=False):
        logger.warning(
            "%s is true but %s is not enabled; graph/node traces will stay disabled.",
            LANGSMITH_OTEL_ENABLED_ENV_VAR,
            LANGSMITH_TRACING_ENV_VAR,
            extra={"runtime_name": runtime_name},
        )
        return False

    if not env.get(LANGSMITH_API_KEY_ENV_VAR, "").strip():
        logger.warning(
            "%s is true but %s is missing; traces cannot be sent.",
            LANGSMITH_OTEL_ENABLED_ENV_VAR,
            LANGSMITH_API_KEY_ENV_VAR,
            extra={"runtime_name": runtime_name},
        )
        return False

    missing_modules = [module for module in _REQUIRED_OTEL_MODULES if not module_exists(module)]
    if missing_modules:
        logger.warning(
            "%s is true but OTEL dependencies are missing (%s). "
            'Install support with `pip install "langsmith[otel]"`.',
            LANGSMITH_OTEL_ENABLED_ENV_VAR,
            ", ".join(missing_modules),
            extra={"runtime_name": runtime_name},
        )
        return False

    project_name = env.get(LANGSMITH_PROJECT_ENV_VAR, "").strip() or "default"
    endpoint = env.get(LANGSMITH_ENDPOINT_ENV_VAR, "").strip() or "https://api.smith.langchain.com"
    logger.info(
        "LangSmith OTEL auto-tracing is enabled (project=%s, endpoint=%s).",
        project_name,
        endpoint,
        extra={"runtime_name": runtime_name},
    )
    return True

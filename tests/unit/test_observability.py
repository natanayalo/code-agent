"""Unit tests for LangSmith OTEL bootstrap helpers."""

from __future__ import annotations

import logging

from apps.observability import (
    LANGSMITH_API_KEY_ENV_VAR,
    LANGSMITH_OTEL_ENABLED_ENV_VAR,
    LANGSMITH_TRACING_ENV_VAR,
    bootstrap_langsmith_otel,
)


def test_bootstrap_langsmith_otel_is_noop_when_otel_is_disabled() -> None:
    """OTEL bootstrap should be inert unless LANGSMITH_OTEL_ENABLED is true."""
    env = {
        LANGSMITH_OTEL_ENABLED_ENV_VAR: "false",
        LANGSMITH_TRACING_ENV_VAR: "true",
        LANGSMITH_API_KEY_ENV_VAR: "lsv2_test",
    }

    enabled = bootstrap_langsmith_otel(
        runtime_name="api",
        logger=logging.getLogger("test"),
        environ=env,
        module_exists=lambda _: True,
    )

    assert enabled is False
    assert env[LANGSMITH_TRACING_ENV_VAR] == "true"


def test_bootstrap_langsmith_otel_enables_tracing_and_reports_ready(caplog) -> None:
    """OTEL bootstrap should force tracing on when omitted and prerequisites are present."""
    caplog.set_level("INFO")
    env = {
        LANGSMITH_OTEL_ENABLED_ENV_VAR: "true",
        LANGSMITH_API_KEY_ENV_VAR: "lsv2_test",
    }

    enabled = bootstrap_langsmith_otel(
        runtime_name="worker",
        logger=logging.getLogger("test"),
        environ=env,
        module_exists=lambda _: True,
    )

    assert enabled is True
    assert env[LANGSMITH_TRACING_ENV_VAR] == "true"
    assert "LangSmith OTEL auto-tracing is enabled" in caplog.text


def test_bootstrap_langsmith_otel_warns_when_tracing_is_disabled(caplog) -> None:
    """OTEL bootstrap should fail soft when tracing is explicitly disabled."""
    caplog.set_level("WARNING")
    env = {
        LANGSMITH_OTEL_ENABLED_ENV_VAR: "true",
        LANGSMITH_TRACING_ENV_VAR: "false",
        LANGSMITH_API_KEY_ENV_VAR: "lsv2_test",
    }

    enabled = bootstrap_langsmith_otel(
        runtime_name="api",
        logger=logging.getLogger("test"),
        environ=env,
        module_exists=lambda _: True,
    )

    assert enabled is False
    assert "graph/node traces will stay disabled" in caplog.text


def test_bootstrap_langsmith_otel_warns_when_deps_are_missing(caplog) -> None:
    """OTEL bootstrap should log an install hint when OTEL deps are absent."""
    caplog.set_level("WARNING")
    env = {
        LANGSMITH_OTEL_ENABLED_ENV_VAR: "true",
        LANGSMITH_API_KEY_ENV_VAR: "lsv2_test",
        LANGSMITH_TRACING_ENV_VAR: "true",
    }

    enabled = bootstrap_langsmith_otel(
        runtime_name="worker",
        logger=logging.getLogger("test"),
        environ=env,
        module_exists=lambda _: False,
    )

    assert enabled is False
    assert 'Install support with `pip install "langsmith[otel]"`' in caplog.text

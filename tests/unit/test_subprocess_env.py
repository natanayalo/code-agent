"""Unit tests for subprocess environment scoping helpers."""

from __future__ import annotations

from workers.subprocess_env import (
    build_antigravity_subprocess_env,
    build_codex_subprocess_env,
    build_gemini_subprocess_env,
)


def test_codex_subprocess_env_keeps_ssl_cert_dir_and_xdg_dirs() -> None:
    """Codex subprocess env should preserve common cert and XDG directory keys."""
    scoped = build_codex_subprocess_env(
        {
            "PATH": "/usr/bin",
            "SSL_CERT_DIR": "/opt/custom-certs",
            "XDG_STATE_HOME": "/tmp/state-home",
            "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
            "UNRELATED_SECRET": "drop-me",
        }
    )

    assert scoped == {
        "PATH": "/usr/bin",
        "SSL_CERT_DIR": "/opt/custom-certs",
        "XDG_STATE_HOME": "/tmp/state-home",
        "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
    }


def test_gemini_subprocess_env_keeps_ssl_cert_dir_and_xdg_dirs() -> None:
    """Gemini subprocess env should preserve common cert and XDG directory keys."""
    scoped = build_gemini_subprocess_env(
        {
            "PATH": "/usr/bin",
            "SSL_CERT_DIR": "/opt/custom-certs",
            "XDG_STATE_HOME": "/tmp/state-home",
            "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
            "UNRELATED_SECRET": "drop-me",
        }
    )

    assert scoped == {
        "PATH": "/usr/bin",
        "SSL_CERT_DIR": "/opt/custom-certs",
        "XDG_STATE_HOME": "/tmp/state-home",
        "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
    }


def test_antigravity_subprocess_env_keeps_keyring_runtime_without_new_secret_scopes() -> None:
    """Antigravity env scoping should preserve runtime dirs but not provider secrets."""
    scoped = build_antigravity_subprocess_env(
        {
            "PATH": "/usr/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
            "GEMINI_HOME": "/workspace/.agent_home/.gemini",
            "GOOGLE_API_KEY": "drop-me",
            "CODE_AGENT_ANTIGRAVITY_AUTH_DIR": "/host/keyring",
        }
    )

    assert scoped == {
        "PATH": "/usr/bin",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/tmp/runtime-dir",
        "GEMINI_HOME": "/workspace/.agent_home/.gemini",
    }


def test_subprocess_env_keeps_trace_headers() -> None:
    """Subprocess env should preserve W3C trace context headers."""
    scoped = build_codex_subprocess_env(
        {
            "TRACEPARENT": "00-test-trace-id-test-span-id-01",
            "TRACESTATE": "vendor=value",
            "BAGGAGE": "key=val",
        }
    )
    assert scoped["TRACEPARENT"] == "00-test-trace-id-test-span-id-01"
    assert scoped["TRACESTATE"] == "vendor=value"
    assert scoped["BAGGAGE"] == "key=val"


def test_subprocess_env_keeps_lc_prefixes() -> None:
    """Subprocess env should preserve LC_ prefixes."""
    scoped = build_gemini_subprocess_env(
        {
            "LC_ALL": "en_US.UTF-8",
            "LC_CTYPE": "C",
            "UNKNOWN": "drop",
        }
    )
    assert scoped["LC_ALL"] == "en_US.UTF-8"
    assert scoped["LC_CTYPE"] == "C"
    assert "UNKNOWN" not in scoped

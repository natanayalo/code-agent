"""Unit tests for subprocess environment scoping helpers."""

from __future__ import annotations

from workers.subprocess_env import build_codex_subprocess_env, build_gemini_subprocess_env


def test_codex_subprocess_env_keeps_ssl_cert_dir_and_xdg_state_home() -> None:
    """Codex subprocess env should preserve common cert and XDG state keys."""
    scoped = build_codex_subprocess_env(
        {
            "PATH": "/usr/bin",
            "SSL_CERT_DIR": "/opt/custom-certs",
            "XDG_STATE_HOME": "/tmp/state-home",
            "UNRELATED_SECRET": "drop-me",
        }
    )

    assert scoped == {
        "PATH": "/usr/bin",
        "SSL_CERT_DIR": "/opt/custom-certs",
        "XDG_STATE_HOME": "/tmp/state-home",
    }


def test_gemini_subprocess_env_keeps_ssl_cert_dir_and_xdg_state_home() -> None:
    """Gemini subprocess env should preserve common cert and XDG state keys."""
    scoped = build_gemini_subprocess_env(
        {
            "PATH": "/usr/bin",
            "SSL_CERT_DIR": "/opt/custom-certs",
            "XDG_STATE_HOME": "/tmp/state-home",
            "UNRELATED_SECRET": "drop-me",
        }
    )

    assert scoped == {
        "PATH": "/usr/bin",
        "SSL_CERT_DIR": "/opt/custom-certs",
        "XDG_STATE_HOME": "/tmp/state-home",
    }

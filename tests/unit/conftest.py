"""Unit-only native runner double.

Production native runs have no host subprocess fallback.  Existing provider
parser tests use small local scripts, so this double keeps those tests focused
on result mapping while executor hardening is covered separately.
"""

from __future__ import annotations

import pytest

import workers.native_agent_runner as native_runner
from tests.native_agent_test_doubles import LocalNativeAgentRunner


@pytest.fixture(autouse=True)
def _use_local_native_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_runner, "DockerNativeAgentExecutor", LocalNativeAgentRunner)


@pytest.fixture(autouse=True)
def _mock_provider_auth_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    auth_base = tmp_path_factory.mktemp("mock_auth")

    gemini_dir = auth_base / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    (gemini_dir / "oauth_creds.json").write_text('{"mock": true}')
    monkeypatch.setenv("CODE_AGENT_GEMINI_AUTH_DIR", str(gemini_dir))

    codex_dir = auth_base / ".codex"
    codex_dir.mkdir(exist_ok=True)
    (codex_dir / "auth.json").write_text('{"mock": true}')
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(codex_dir))

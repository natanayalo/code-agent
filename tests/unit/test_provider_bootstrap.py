"""Provider bootstrap authentication-mode and auth-directory tests."""

from pathlib import Path

import pytest

from orchestrator.provider_diagnostics_credentials import (
    resolve_antigravity_token_path,
    resolve_codex_auth_path,
)
from sandbox.provider_bootstrap import (
    ProviderBootstrapError,
    ProviderBootstrapLoader,
    resolve_antigravity_provider_dir,
    resolve_codex_provider_dir,
)
from workers.antigravity_cli_adapter import AntigravityCliRuntimeAdapter
from workers.gemini_cli_worker_native import _antigravity_provider_dir


def _write_antigravity_token(provider_dir: Path) -> None:
    token_path = provider_dir / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("token", encoding="utf-8")


def test_codex_auth_resolution_prefers_configured_directory(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".codex"
    codex_home = tmp_path / "codex-home" / ".codex"
    configured.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    (configured / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(configured))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert resolve_codex_provider_dir() == configured
    assert resolve_codex_auth_path() == configured / "auth.json"


def test_codex_auth_resolution_uses_fallback_directory_consistently(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".codex"
    codex_home = tmp_path / "codex-home" / ".codex"
    configured.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(configured))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved_dir = resolve_codex_provider_dir()
    assert resolved_dir == codex_home
    assert resolve_codex_auth_path() == codex_home / "auth.json"
    assert ProviderBootstrapLoader.load(resolved_dir).ref_names == ("codex_auth_json",)


def test_codex_api_key_resolution_keeps_first_configured_directory(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".codex"
    fallback = tmp_path / "fallback" / ".codex"
    configured.mkdir(parents=True)
    fallback.mkdir(parents=True)
    (configured / "config.toml").write_text('model = "gpt-5"', encoding="utf-8")
    (fallback / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(configured))
    monkeypatch.setenv("CODEX_HOME", str(fallback))

    assert resolve_codex_provider_dir(required_file=None) == configured


def test_antigravity_auth_resolution_prefers_configured_directory(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".gemini"
    gemini_home = tmp_path / "gemini-home" / ".gemini"
    _write_antigravity_token(configured)
    _write_antigravity_token(gemini_home)
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(configured))
    monkeypatch.setenv("GEMINI_HOME", str(gemini_home))
    adapter = AntigravityCliRuntimeAdapter(env={"GEMINI_HOME": str(gemini_home)})

    assert resolve_antigravity_provider_dir() == configured
    assert resolve_antigravity_token_path() == (
        configured / "antigravity-cli/antigravity-oauth-token"
    )
    assert _antigravity_provider_dir(adapter) == configured


def test_antigravity_auth_resolution_uses_fallback_directory_consistently(
    tmp_path, monkeypatch
) -> None:
    configured = tmp_path / "configured" / ".gemini"
    fallback = tmp_path / "fallback" / ".gemini"
    configured.mkdir(parents=True)
    _write_antigravity_token(fallback)
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(configured))
    monkeypatch.delenv("GEMINI_HOME", raising=False)
    monkeypatch.setenv("CODE_AGENT_GEMINI_AUTH_DIR", str(fallback))
    adapter = AntigravityCliRuntimeAdapter(env={})

    assert resolve_antigravity_provider_dir() == fallback
    assert resolve_antigravity_token_path() == (
        fallback / "antigravity-cli/antigravity-oauth-token"
    )
    assert _antigravity_provider_dir(adapter) == fallback


def test_codex_api_key_mode_does_not_mount_oauth_auth_file(tmp_path) -> None:
    provider_dir = tmp_path / ".codex"
    provider_dir.mkdir()
    (provider_dir / "auth.json").write_text('{"oauth": "secret"}', encoding="utf-8")
    (provider_dir / "config.toml").write_text('model = "gpt-5"', encoding="utf-8")

    bootstrap = ProviderBootstrapLoader.load(provider_dir, has_api_key=True)

    assert bootstrap.ref_names == ("codex_config_toml",)
    assert "codex_auth_json" not in bootstrap.file_store


def test_gemini_api_key_mode_does_not_mount_oauth_credentials(tmp_path) -> None:
    provider_dir = tmp_path / ".gemini"
    provider_dir.mkdir()
    (provider_dir / "oauth_creds.json").write_text('{"oauth": "secret"}', encoding="utf-8")
    (provider_dir / "settings.json").write_text("{}", encoding="utf-8")

    bootstrap = ProviderBootstrapLoader.load(provider_dir, has_api_key=True)

    assert bootstrap.ref_names == ("gemini_settings",)
    assert "gemini_oauth_creds" not in bootstrap.file_store


def test_antigravity_bootstrap_stages_only_nested_oauth_token(tmp_path) -> None:
    provider_dir = tmp_path / ".gemini"
    token_path = provider_dir / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text('{"token":{"access_token":"access","refresh_token":"refresh"}}')
    (provider_dir / "oauth_creds.json").write_text('{"generic":"oauth"}')

    bootstrap = ProviderBootstrapLoader.load_antigravity(provider_dir)

    assert bootstrap.ref_names == ("antigravity_oauth_token",)
    assert bootstrap.destination_by_ref == {
        "antigravity_oauth_token": ".gemini/antigravity-cli/antigravity-oauth-token"
    }
    assert "generic" not in "".join(bootstrap.file_store)


@pytest.mark.parametrize("kind", ["missing", "unreadable"])
def test_antigravity_bootstrap_requires_readable_oauth_token(tmp_path, monkeypatch, kind) -> None:
    provider_dir = tmp_path / ".gemini"
    token_path = provider_dir / "antigravity-cli" / "antigravity-oauth-token"
    token_path.parent.mkdir(parents=True)
    if kind == "unreadable":
        token_path.write_text("token")
        monkeypatch.setattr(
            "pathlib.Path.read_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
        )

    with pytest.raises(ProviderBootstrapError, match="Antigravity OAuth token"):
        ProviderBootstrapLoader.load_antigravity(provider_dir)

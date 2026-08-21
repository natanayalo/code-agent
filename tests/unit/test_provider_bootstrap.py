"""Provider bootstrap authentication-mode tests."""

from sandbox.provider_bootstrap import ProviderBootstrapLoader


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

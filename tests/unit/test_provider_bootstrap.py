"""Provider bootstrap authentication-mode tests."""

import pytest

from sandbox.provider_bootstrap import ProviderBootstrapError, ProviderBootstrapLoader


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

"""Regression coverage for AGY's provider-specific credential handoff."""

import json
from pathlib import Path

import pytest

from sandbox.redact import SecretRedactor
from tests.unit.test_antigravity_cli_worker_native import _make_worker
from workers import AntigravityCliRuntimeAdapter
from workers.antigravity_cli_worker_native import prepare_antigravity_workspace_migration
from workers.gemini_cli_worker_native import (
    _antigravity_provider_dir,
    _register_antigravity_token_fields,
)


def test_explicit_agy_auth_directory_precedes_adapter_home(tmp_path, monkeypatch):
    configured = tmp_path / "configured"
    token = configured / "antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("fake")
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(configured))
    adapter = AntigravityCliRuntimeAdapter(env={"GEMINI_HOME": str(tmp_path / "other")})
    assert _antigravity_provider_dir(adapter) == configured


def test_unmounted_host_auth_path_falls_back_to_available_adapter_path(tmp_path, monkeypatch):
    worker, _ = _make_worker(tmp_path)
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(tmp_path / "absent-host-path"))
    assert _antigravity_provider_dir(worker.runtime_adapter) == Path(
        worker.runtime_adapter.env["GEMINI_HOME"]
    )


@pytest.mark.parametrize("payload", ["not-json", "[]", "{}", '{"token":false}'])
def test_redaction_handles_malformed_token(payload):
    _register_antigravity_token_fields(SecretRedactor(), payload)


def test_nested_oauth_fields_are_redacted():
    redactor = SecretRedactor()
    _register_antigravity_token_fields(
        redactor,
        json.dumps({"token": {"access_token": "fake-access", "refresh_token": "fake-refresh"}}),
    )
    assert "fake-access" not in redactor.redact("error fake-access")
    assert "fake-refresh" not in redactor.redact("error fake-refresh")


@pytest.mark.parametrize("unlink_failure", [False, True])
def test_legacy_token_symlink_removed_or_fails_closed(tmp_path, monkeypatch, unlink_failure):
    worker, workspace = _make_worker(tmp_path)
    source = (
        Path(worker.runtime_adapter.env["GEMINI_HOME"]) / "antigravity-cli/antigravity-oauth-token"
    )
    original = source.read_bytes()
    target = (
        workspace.workspace_path / ".agent_home/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    target.parent.mkdir(parents=True)
    target.symlink_to(source)
    if unlink_failure:
        original_unlink = Path.unlink

        def guarded_unlink(path, *args, **kwargs):
            if path == target:
                raise PermissionError("test denied")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", guarded_unlink)
        with pytest.raises(RuntimeError, match="Cannot safely remove"):
            prepare_antigravity_workspace_migration(
                adapter=worker.runtime_adapter, workspace=workspace
            )
    else:
        prepare_antigravity_workspace_migration(adapter=worker.runtime_adapter, workspace=workspace)
        assert not target.is_symlink()
        assert not target.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("source", ["environment", "gemini_config", "home", "missing", "denied"])
def test_agy_discovery_fallbacks(tmp_path, monkeypatch, source):
    for key in ("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", "GEMINI_HOME", "CODE_AGENT_GEMINI_AUTH_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    directory = tmp_path / ".gemini"
    if source == "environment":
        monkeypatch.setenv("GEMINI_HOME", str(directory))
    elif source == "gemini_config":
        monkeypatch.setenv("CODE_AGENT_GEMINI_AUTH_DIR", str(directory))
    if source not in ("missing", "denied"):
        token = directory / "antigravity-cli/antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.write_text("fake")
    else:

        def unavailable(_path):
            if source == "denied":
                raise PermissionError("test denied")
            return False

        monkeypatch.setattr(Path, "is_file", unavailable)
    assert _antigravity_provider_dir(AntigravityCliRuntimeAdapter()) == directory


def test_redaction_ignores_non_string_and_empty_token_fields():
    redactor = SecretRedactor()
    _register_antigravity_token_fields(
        redactor, '{"token":{"access_token":null,"refresh_token":""}}'
    )
    assert redactor.redact("ordinary message") == "ordinary message"


def test_agy_endpoint_does_not_expand_gemini_hosts():
    from sandbox.provider_hosts import ANTIGRAVITY_OAUTH_HOSTS, GEMINI_OAUTH_HOSTS

    assert set(ANTIGRAVITY_OAUTH_HOSTS) - set(GEMINI_OAUTH_HOSTS) == {
        "daily-cloudcode-pa.googleapis.com"
    }

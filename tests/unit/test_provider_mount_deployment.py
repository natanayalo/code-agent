"""Task credential deployment boundary contracts for the PR #430 split."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_provider_credentials_are_individual_readonly_mounts():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mounts = [m for m in compose["services"]["worker"]["volumes"] if isinstance(m, dict)]
    assert len(mounts) == 2
    assert all(m["read_only"] and not m["bind"]["create_host_path"] for m in mounts)
    assert {m["target"] for m in mounts} == {
        "/root/.codex/auth.json",
        "/root/.gemini/antigravity-cli/antigravity-oauth-token",
    }


def test_build_context_excludes_credentials():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert {
        ".env*",
        "**/.env*",
        "**/auth.json",
        "**/oauth_creds.json",
        "**/antigravity-oauth-token",
        ".codex/",
        ".gemini/",
        "artifacts/",
    } <= set(patterns)
    assert "!cert.pem" in patterns

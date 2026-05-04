"""Unit tests for sandbox hardening and audit utilities."""

from __future__ import annotations

from sandbox.policy import PathPolicy
from sandbox.redact import (
    SecretRedactor,
    construct_sandbox_output,
    mask_url_credentials,
    sanitize_command,
)


def test_secret_redactor_basic():
    redactor = SecretRedactor(["secret1", "password"])
    assert redactor.redact("my secret1 is password") == "my [REDACTED] is [REDACTED]"
    assert redactor.redact("nothing to see here") == "nothing to see here"
    assert redactor.redact("") == ""


def test_secret_redactor_overlapping():
    redactor = SecretRedactor(["secret", "secrets"])
    # Longer secrets should be replaced first to avoid partial masking.
    assert redactor.redact("my secrets") == "my [REDACTED]"
    assert redactor.redact("my secret") == "my [REDACTED]"


def test_secret_redactor_empty_or_whitespace():
    redactor = SecretRedactor(["", "  ", "valid"])
    assert redactor.redact("valid and ") == "[REDACTED] and "


def test_path_policy_validation():
    policy = PathPolicy(
        allowed_prefixes=["/workspace/repo"],
        denied_prefixes=["/workspace/repo/.git", "/workspace/repo/secrets.txt"],
    )
    # Allowed path
    assert policy.check_path("/workspace/repo/src/main.py") is True
    # Denied paths
    assert policy.check_path("/workspace/repo/.git/config") is False
    assert policy.check_path("/workspace/repo/secrets.txt") is False
    assert policy.check_path("/workspace/repo/secrets.txt/nested") is False
    # Path outside allowed prefixes
    assert policy.check_path("/etc/passwd") is False
    assert policy.check_path("/workspace/other") is False


def test_path_policy_default():
    policy = PathPolicy()
    assert policy.check_path("/workspace/.git") is False
    assert policy.check_path("/root") is False


def test_path_policy_traversal():
    policy = PathPolicy(allowed_prefixes=["/workspace"], denied_prefixes=["/workspace/.git"])
    # Test path traversal attempts
    assert policy.check_path("/workspace/repo/../../etc/passwd") is False
    assert policy.check_path("/workspace/../../etc/shadow") is False
    assert policy.check_path("/workspace/repo/../.git/config") is False


def test_path_policy_robustness():
    policy = PathPolicy(allowed_prefixes=["/workspace"], denied_prefixes=["/workspace/.git"])
    # Correctly allowed (no longer blocked by prefix matching)
    assert policy.check_path("/workspace/.git_config") is True
    # Correctly denied
    assert policy.check_path("/workspace/.git/config") is False
    assert policy.check_path("/workspace/.git") is False


def test_mask_url_credentials():
    assert mask_url_credentials("https://user:pass@github.com") == "https://****@github.com"
    assert (
        mask_url_credentials("git clone https://user:pass@github.com/repo.git")
        == "git clone https://****@github.com/repo.git"
    )
    assert mask_url_credentials("plain text") == "plain text"


def test_sanitize_command_redacts_both():
    redactor = SecretRedactor(["my-secret"])
    cmd = "git clone https://user:pass@github.com/repo.git && echo my-secret"
    sanitized = sanitize_command(cmd, redactor)
    assert "https://****@github.com" in sanitized
    assert "my-secret" not in sanitized
    assert "[REDACTED]" in sanitized


def test_construct_sandbox_output_redacts_both():
    redactor = SecretRedactor(["my-secret"])
    stdout = "cloning https://user:pass@github.com/repo.git"
    stderr = "failed with my-secret"
    output = construct_sandbox_output(stdout, stderr, redactor)
    assert "https://****@github.com" in output
    assert "my-secret" not in output
    assert "[REDACTED]" in output

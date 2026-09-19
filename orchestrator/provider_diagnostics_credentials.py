"""Credential validation helpers for provider pre-dispatch diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator.provider_diagnostics_types import (
    DiagnosticCheckResult,
    ProviderExecutionContext,
)
from sandbox.secrets import SecretRegistry

_MAX_DETAIL_LENGTH = 120


def _truncate_exception(exc: Exception) -> str:
    """Bound exception text before including it in operator-facing diagnostics."""
    detail = str(exc)
    if len(detail) <= _MAX_DETAIL_LENGTH:
        return detail
    return f"{detail[:_MAX_DETAIL_LENGTH - 3]}..."


def resolve_codex_auth_path() -> Path:
    """Return effective Codex auth.json path based on environment precedence."""
    configured = os.environ.get("CODE_AGENT_CODEX_AUTH_DIR")
    if configured:
        return Path(configured).expanduser() / "auth.json"
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def resolve_antigravity_token_path() -> Path:
    """Return effective Antigravity OAuth token path based on environment precedence."""
    configured = os.environ.get("CODE_AGENT_ANTIGRAVITY_AUTH_DIR")
    if configured:
        return Path(configured).expanduser() / "antigravity-cli" / "antigravity-oauth-token"
    gemini_home = os.environ.get("GEMINI_HOME")
    if gemini_home:
        return Path(gemini_home).expanduser() / "antigravity-cli" / "antigravity-oauth-token"
    return Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def check_codex_credentials(
    context: ProviderExecutionContext, secret_registry: SecretRegistry
) -> DiagnosticCheckResult:
    """Validate Codex credentials for either API key or ChatGPT OAuth mode."""
    if context.auth_mechanism == "api_key":
        has_ref = "openai_api_key" in [r.lower() for r in context.credential_refs]
        reg_def = secret_registry.get("openai_api_key")
        has_env = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        if has_ref and reg_def is None:
            return DiagnosticCheckResult(
                name="credentials",
                category="credentials",
                status="unready",
                detail="Registered OpenAI API key reference is missing from SecretRegistry.",
                remediation="Register secret 'openai_api_key' in SecretRegistry.",
                verification_scope="local_presence",
                blocking=True,
            )
        if not has_ref and not has_env:
            return DiagnosticCheckResult(
                name="credentials",
                category="credentials",
                status="unready",
                detail="Codex API key is not configured in secret references or environment.",
                remediation="Set OPENAI_API_KEY or register an authorized 'openai_api_key' secret.",
                verification_scope="local_presence",
                blocking=True,
            )
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="ready",
            detail="OpenAI API key configured for Codex execution.",
            verification_scope="local_presence",
            blocking=False,
        )

    auth_file = resolve_codex_auth_path()
    if not auth_file.is_file():
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail="Codex OAuth auth.json not found in the configured auth directory.",
            remediation=(
                "Run: 'docker compose run --rm --no-deps worker codex login' "
                "or set CODE_AGENT_CODEX_AUTH_DIR."
            ),
            verification_scope="local_presence",
            blocking=True,
        )
    try:
        content = auth_file.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError("auth.json file is empty")
        json.loads(content)
    except Exception as exc:
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail=f"Codex auth.json is malformed or unreadable: {_truncate_exception(exc)}",
            remediation=(
                "Re-authenticate with 'docker compose run --rm --no-deps worker codex login'."
            ),
            verification_scope="local_structure",
            blocking=True,
        )

    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail="Codex OAuth credentials present and structured correctly.",
        verification_scope="local_structure",
        blocking=False,
    )


def check_antigravity_credentials(context: ProviderExecutionContext) -> DiagnosticCheckResult:
    """Validate that native Antigravity has a valid antigravity-oauth-token file."""
    token_path = resolve_antigravity_token_path()
    if not token_path.is_file():
        gemini_dir = token_path.parents[1]
        if (gemini_dir / "oauth_creds.json").is_file():
            return DiagnosticCheckResult(
                name="credentials",
                category="credentials",
                status="unready",
                detail=(
                    "Found oauth_creds.json, but native Antigravity requires "
                    "antigravity-oauth-token. The token file is missing."
                ),
                remediation=(
                    "Run 'scripts/bootstrap_antigravity_auth.sh' with "
                    "CODE_AGENT_ANTIGRAVITY_AUTH_DIR set."
                ),
                verification_scope="local_presence",
                blocking=True,
            )
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail=(
                "Antigravity OAuth token not found (token file absent from the configured "
                "auth directory)."
            ),
            remediation=(
                "Run 'scripts/bootstrap_antigravity_auth.sh' with "
                "CODE_AGENT_ANTIGRAVITY_AUTH_DIR set."
            ),
            verification_scope="local_presence",
            blocking=True,
        )

    try:
        content = token_path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError("antigravity-oauth-token file is empty")
    except Exception as exc:
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail=f"Antigravity token file is unreadable: {_truncate_exception(exc)}",
            remediation="Run 'scripts/bootstrap_antigravity_auth.sh' to recreate the token.",
            verification_scope="local_structure",
            blocking=True,
        )

    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail="Antigravity OAuth token present and readable.",
        verification_scope="local_structure",
        blocking=False,
    )


def check_openrouter_credentials(
    context: ProviderExecutionContext, secret_registry: SecretRegistry
) -> DiagnosticCheckResult:
    """Validate OpenRouter API key configuration in registered secrets or environment."""
    has_ref = "openrouter_api_key" in [r.lower() for r in context.credential_refs]
    reg_def = secret_registry.get("openrouter_api_key")
    if has_ref and reg_def is None:
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail="Registered OpenRouter API key reference is missing from SecretRegistry.",
            remediation="Register secret 'openrouter_api_key' in SecretRegistry.",
            verification_scope="local_presence",
            blocking=True,
        )

    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not has_ref and not env_key:
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="unready",
            detail="OpenRouter API key is not configured in secret references or environment.",
            remediation="Set OPENROUTER_API_KEY in the environment or worker secrets.",
            verification_scope="local_presence",
            blocking=True,
        )

    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail="OpenRouter API key configured.",
        verification_scope="local_presence",
        blocking=False,
    )


def check_provider_credentials(
    context: ProviderExecutionContext, secret_registry: SecretRegistry
) -> DiagnosticCheckResult:
    """Entrypoint for validating credentials across all provider types."""
    if context.provider == "codex":
        return check_codex_credentials(context, secret_registry)
    if context.provider == "antigravity":
        return check_antigravity_credentials(context)
    if context.provider == "openrouter":
        return check_openrouter_credentials(context, secret_registry)
    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail=f"No provider credential required for '{context.provider}'.",
        verification_scope="local_presence",
        blocking=False,
    )

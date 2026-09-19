"""Credential validation helpers for provider pre-dispatch diagnostics."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from orchestrator.provider_diagnostics_types import (
    DiagnosticCheckResult,
    ProviderExecutionContext,
    VerificationScope,
)
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRegistry,
    SecretScope,
    SecretSource,
)


def _credential_error_category(exc: Exception) -> str:
    """Return an allowlisted error category without exposing exception contents."""
    if isinstance(exc, ValueError):
        return "invalid_format"
    if isinstance(exc, OSError):
        return "unreadable"
    return "resolution_failed"


def _unready(
    detail: str,
    remediation: str,
    *,
    verification_scope: VerificationScope = "local_presence",
) -> DiagnosticCheckResult:
    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="unready",
        detail=detail,
        remediation=remediation,
        verification_scope=verification_scope,
        blocking=True,
    )


def _unknown(
    detail: str,
    remediation: str,
    *,
    verification_scope: VerificationScope = "local_presence",
) -> DiagnosticCheckResult:
    """Report a supported check whose backing material is outside local visibility."""
    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="unknown",
        detail=detail,
        remediation=remediation,
        verification_scope=verification_scope,
        blocking=True,
    )


def _is_api_key_definition(definition: RegisteredSecretDefinition, expected_env_var: str) -> bool:
    return bool(
        definition
        and (
            definition.source_key.upper() == expected_env_var
            or (definition.destination_env_var or "").upper() == expected_env_var
        )
    )


def _source_value(
    definition: RegisteredSecretDefinition,
    *,
    secret_env: Mapping[str, str],
    secret_store: Mapping[str, str] | None,
    file_store: Mapping[str, str] | None,
    ephemeral_store: object | None,
    task_id: str | None,
) -> tuple[bool, str | None]:
    """Return source inspectability and material without exposing the material."""
    if definition.source == SecretSource.ENV:
        return True, secret_env.get(definition.source_key)
    if definition.source == SecretSource.SECRET_STORE:
        if secret_store is None:
            return False, None
        return True, secret_store.get(definition.source_key)
    if definition.source == SecretSource.FILE:
        if file_store is None:
            return False, None
        return True, file_store.get(definition.source_key)
    if definition.source == SecretSource.EPHEMERAL:
        if ephemeral_store is None:
            return False, None
        try:
            if isinstance(ephemeral_store, Mapping):
                return True, ephemeral_store.get(definition.source_key)
            getter = getattr(ephemeral_store, "get", None)
            if getter is None:
                return False, None
            return True, getter(definition.source_key, task_id=task_id)
        except Exception:
            return False, None
    return False, None


def _validate_api_key_definition(
    context: ProviderExecutionContext,
    definition: RegisteredSecretDefinition,
    *,
    expected_env_var: str,
    provider_label: str,
    secret_env: Mapping[str, str],
    secret_store: Mapping[str, str] | None,
    file_store: Mapping[str, str] | None,
    ephemeral_store: object | None,
) -> DiagnosticCheckResult:
    """Validate scope, delivery destination, and source resolution for one definition."""
    if definition.required_scope != SecretScope.PROVIDER_AUTH:
        return _unready(
            f"Registered {provider_label} API key lacks provider_auth scope.",
            "Register the provider key with required_scope=provider_auth.",
        )
    if definition.exposure_policy not in (
        SecretExposurePolicy.SANDBOX_ENV,
        SecretExposurePolicy.SANDBOX_FILE,
    ):
        return _unready(
            f"Registered {provider_label} API key is not exposed to the worker sandbox.",
            "Use exposure_policy=sandbox_env or sandbox_file for provider execution.",
        )
    if definition.exposure_policy != SecretExposurePolicy.SANDBOX_ENV:
        return _unready(
            f"Registered {provider_label} API key is file-only, but the provider consumes "
            f"{expected_env_var} from its environment.",
            f"Expose the {provider_label} API key with destination_env_var={expected_env_var}.",
        )
    if definition.destination_env_var != expected_env_var:
        return _unready(
            f"Registered {provider_label} API key is delivered to the wrong environment "
            f"variable; expected {expected_env_var}.",
            f"Set destination_env_var={expected_env_var} for the provider key.",
        )

    source_inspectable, source_value = _source_value(
        definition,
        secret_env=secret_env,
        secret_store=secret_store,
        file_store=file_store,
        ephemeral_store=ephemeral_store,
        task_id=context.task_id,
    )
    if not source_inspectable:
        return _unknown(
            f"Registered {provider_label} API key source resolution is not locally inspectable.",
            "Verify the configured worker secret resolver can resolve the provider key "
            "before dispatch.",
        )
    if not str(source_value or "").strip():
        return _unready(
            f"Registered {provider_label} API key source is empty.",
            f"Populate the configured source for the {provider_label} API key before "
            "dispatching the provider.",
        )
    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail=f"{provider_label} API key resolved for provider execution.",
        verification_scope="local_presence",
        blocking=False,
    )


def _registered_api_key_check(
    context: ProviderExecutionContext,
    secret_registry: SecretRegistry,
    secret_env: Mapping[str, str],
    *,
    expected_env_var: str,
    provider_label: str,
    secret_store: Mapping[str, str] | None = None,
    file_store: Mapping[str, str] | None = None,
    ephemeral_store: object | None = None,
) -> DiagnosticCheckResult | None:
    """Validate a referenced provider key against its actual registered source."""
    if not context.credential_refs:
        return None

    matching: list[RegisteredSecretDefinition] = []
    missing_refs: list[str] = []
    for ref in context.credential_refs:
        definition = secret_registry.get(ref, task_id=context.task_id)
        if definition is None:
            missing_refs.append(ref)
        elif _is_api_key_definition(definition, expected_env_var):
            matching.append(definition)

    if not matching:
        if missing_refs:
            return _unready(
                f"Registered {provider_label} API key reference is missing from SecretRegistry.",
                f"Register the referenced {provider_label} secret in SecretRegistry.",
            )
        return _unready(
            f"Referenced secret does not map to {expected_env_var} for {provider_label}.",
            f"Reference a registered {provider_label} secret backed by {expected_env_var}.",
        )

    last_failure: DiagnosticCheckResult | None = None
    for definition in matching:
        result = _validate_api_key_definition(
            context,
            definition,
            expected_env_var=expected_env_var,
            provider_label=provider_label,
            secret_env=secret_env,
            secret_store=secret_store,
            file_store=file_store,
            ephemeral_store=ephemeral_store,
        )
        if result.status == "ready":
            return result
        last_failure = result

    return last_failure


def _effective_api_key_check(
    *,
    configured: bool,
    expected_env_var: str,
    provider_label: str,
) -> DiagnosticCheckResult:
    if configured:
        return DiagnosticCheckResult(
            name="credentials",
            category="credentials",
            status="ready",
            detail=f"{provider_label} API key is configured for provider execution.",
            verification_scope="local_presence",
            blocking=False,
        )
    return _unready(
        f"{provider_label} API key is not configured for provider execution.",
        f"Set {expected_env_var} in the effective worker configuration.",
    )


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
    context: ProviderExecutionContext,
    secret_registry: SecretRegistry,
    *,
    secret_env: Mapping[str, str] | None = None,
    effective_api_key_configured: bool | None = None,
    secret_store: Mapping[str, str] | None = None,
    file_store: Mapping[str, str] | None = None,
    ephemeral_store: object | None = None,
) -> DiagnosticCheckResult:
    """Validate Codex credentials for either API key or ChatGPT OAuth mode."""
    if context.auth_mechanism == "api_key":
        env = os.environ if secret_env is None else secret_env
        referenced = _registered_api_key_check(
            context,
            secret_registry,
            env,
            expected_env_var="OPENAI_API_KEY",
            provider_label="OpenAI",
            secret_store=secret_store,
            file_store=file_store,
            ephemeral_store=ephemeral_store,
        )
        if referenced is not None:
            return referenced
        configured = (
            effective_api_key_configured
            if effective_api_key_configured is not None
            else bool(str(env.get("OPENAI_API_KEY", "")).strip())
        )
        return _effective_api_key_check(
            configured=configured,
            expected_env_var="OPENAI_API_KEY",
            provider_label="OpenAI",
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
            detail=(
                f"Codex auth.json is malformed or unreadable: {_credential_error_category(exc)}"
            ),
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
            detail=(f"Antigravity token file is unreadable: {_credential_error_category(exc)}"),
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
    context: ProviderExecutionContext,
    secret_registry: SecretRegistry,
    *,
    secret_env: Mapping[str, str] | None = None,
    effective_api_key_configured: bool | None = None,
    secret_store: Mapping[str, str] | None = None,
    file_store: Mapping[str, str] | None = None,
    ephemeral_store: object | None = None,
) -> DiagnosticCheckResult:
    """Validate OpenRouter API key configuration in registered secrets or environment."""
    env = os.environ if secret_env is None else secret_env
    referenced = _registered_api_key_check(
        context,
        secret_registry,
        env,
        expected_env_var="OPENROUTER_API_KEY",
        provider_label="OpenRouter",
        secret_store=secret_store,
        file_store=file_store,
        ephemeral_store=ephemeral_store,
    )
    if referenced is not None:
        return referenced
    configured = (
        effective_api_key_configured
        if effective_api_key_configured is not None
        else bool(str(env.get("OPENROUTER_API_KEY", "")).strip())
    )
    return _effective_api_key_check(
        configured=configured,
        expected_env_var="OPENROUTER_API_KEY",
        provider_label="OpenRouter",
    )


def check_provider_credentials(
    context: ProviderExecutionContext,
    secret_registry: SecretRegistry,
    *,
    secret_env: Mapping[str, str] | None = None,
    effective_api_key_configured: bool | None = None,
    secret_store: Mapping[str, str] | None = None,
    file_store: Mapping[str, str] | None = None,
    ephemeral_store: object | None = None,
) -> DiagnosticCheckResult:
    """Entrypoint for validating credentials across all provider types."""
    if context.provider == "codex":
        return check_codex_credentials(
            context,
            secret_registry,
            secret_env=secret_env,
            effective_api_key_configured=effective_api_key_configured,
            secret_store=secret_store,
            file_store=file_store,
            ephemeral_store=ephemeral_store,
        )
    if context.provider == "antigravity":
        return check_antigravity_credentials(context)
    if context.provider == "openrouter":
        return check_openrouter_credentials(
            context,
            secret_registry,
            secret_env=secret_env,
            effective_api_key_configured=effective_api_key_configured,
            secret_store=secret_store,
            file_store=file_store,
            ephemeral_store=ephemeral_store,
        )
    return DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail=f"No provider credential required for '{context.provider}'.",
        verification_scope="local_presence",
        blocking=False,
    )

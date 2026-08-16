"""Legacy ingress migration contracts and pre-validation sanitization."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.secrets import (
    ALLOWED_SECRET_ENV_VARS,
    CapabilityViolationError,
    EphemeralSecretStore,
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretScope,
    SecretSource,
)

_RE_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ConflictingSecretDeclarationError(CapabilityViolationError):
    """Raised when legacy secrets and modern secret_refs declare conflicting keys."""


class DeprecatedLegacySecretsError(CapabilityViolationError):
    """Raised when legacy raw secrets are used after the deprecation cutoff."""


def sanitize_legacy_ingress_payload(
    payload: Any,
    *,
    ephemeral_store: EphemeralSecretStore | dict[str, str] | None = None,
) -> dict[str, Any]:
    """Sanitize raw transport payload before Pydantic model validation.

    Ensures that any non-dict secrets payload or raw secret values are safely
    scrubbed from the input dictionary so Pydantic ValidationError can never
    capture or echo raw secret material in error contexts.
    If an ephemeral_store is provided, raw secret values are captured in-memory
    prior to placeholder replacement.
    """
    if not isinstance(payload, dict):
        raise ValueError("Ingress task payload must be a JSON object / dictionary")

    sanitized = dict(payload)
    if "secrets" in sanitized:
        raw_secrets = sanitized["secrets"]
        if isinstance(raw_secrets, dict):
            if ephemeral_store is not None:
                for k, v in raw_secrets.items():
                    if isinstance(v, str) and v and v != "[REDACTED_AT_INGRESS]":
                        if isinstance(ephemeral_store, EphemeralSecretStore):
                            ephemeral_store.store(str(k), v)
                        elif isinstance(ephemeral_store, dict):
                            ephemeral_store[str(k)] = v
            # Scrub values to placeholders, preserving keys for reference extraction
            sanitized["secrets"] = {str(k): "[REDACTED_AT_INGRESS]" for k in raw_secrets}
        else:
            # Replace malformed non-dict secrets with empty dict and reject
            sanitized["secrets"] = {}
            raise ValueError(
                "Invalid secrets payload: secrets must be a dictionary of key-value pairs"
            )
    return sanitized


class LegacyIngressTaskRequest(BaseModel):
    """Ingress-only task request DTO for legacy raw-secret intake."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    task_text: str = Field(min_length=1)
    secret_refs: tuple[SecretRef, ...] = ()
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_raw_payload(
        cls,
        raw_payload: Any,
        *,
        ephemeral_store: EphemeralSecretStore | dict[str, str] | None = None,
    ) -> LegacyIngressTaskRequest:
        """Construct request from raw transport payload after pre-validation sanitization."""
        sanitized = sanitize_legacy_ingress_payload(raw_payload, ephemeral_store=ephemeral_store)
        return cls.model_validate(sanitized)

    @model_validator(mode="before")
    @classmethod
    def _scrub_raw_secret_values(cls, data: Any) -> Any:
        if isinstance(data, dict) and "secrets" in data:
            raw_secrets = data["secrets"]
            if isinstance(raw_secrets, dict):
                scrubbed_secrets = {}
                for k in raw_secrets:
                    scrubbed_secrets[str(k)] = "[REDACTED_AT_INGRESS]"
                data = dict(data)
                data["secrets"] = scrubbed_secrets
            else:
                data = dict(data)
                data["secrets"] = {}
                raise ValueError(
                    "Invalid secrets payload: secrets must be a dictionary of key-value pairs"
                )
        return data

    @field_validator("secrets")
    @classmethod
    def _sanitize_legacy_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _RE_SAFE_NAME.match(key):
                raise ValueError("Invalid secret key format in legacy secrets")
        return v


class IngressMigrationAdapter:
    """Adapts legacy ingress requests into ephemeral store entries and SecretRefs."""

    @classmethod
    def adapt(
        cls,
        request_or_payload: LegacyIngressTaskRequest | dict[str, Any],
        *,
        ephemeral_store: EphemeralSecretStore | dict[str, str] | None = None,
        reject_legacy_secrets: bool = False,
    ) -> tuple[SecretRef, ...]:
        """Extract logical secret names, placing legacy raw values into the ephemeral store."""
        if isinstance(request_or_payload, dict):
            request = LegacyIngressTaskRequest.from_raw_payload(
                request_or_payload, ephemeral_store=ephemeral_store
            )
        else:
            request = request_or_payload

        if reject_legacy_secrets and request.secrets:
            raise DeprecatedLegacySecretsError(
                "Legacy raw secrets are no longer accepted. Use secret_refs instead."
            )

        ref_names = {ref.name for ref in request.secret_refs}
        conflicts = ref_names.intersection(request.secrets.keys())
        if conflicts:
            raise ConflictingSecretDeclarationError(
                f"Conflicting secret declarations for keys: {sorted(conflicts)}"
            )

        adapted_refs = list(request.secret_refs)
        for name, raw_val in request.secrets.items():
            adapted_refs.append(SecretRef(name=name))
            if ephemeral_store is not None and raw_val and raw_val != "[REDACTED_AT_INGRESS]":
                if isinstance(ephemeral_store, EphemeralSecretStore):
                    ephemeral_store.store(name, raw_val)
                elif isinstance(ephemeral_store, dict):
                    ephemeral_store[name] = raw_val

        return tuple(adapted_refs)

    @classmethod
    def adapt_and_register_ephemeral(
        cls,
        request_or_payload: LegacyIngressTaskRequest | dict[str, Any],
        *,
        registry: SecretRegistry,
        ephemeral_store: EphemeralSecretStore,
        scope: SecretScope = SecretScope.CUSTOM,
        exposure_policy: SecretExposurePolicy = SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts: Sequence[str] = (),
    ) -> tuple[SecretRef, ...]:
        """Adapt request, store raw secrets in ephemeral store, and register definitions."""
        refs = cls.adapt(request_or_payload, ephemeral_store=ephemeral_store)
        if isinstance(request_or_payload, dict):
            raw_secrets = request_or_payload.get("secrets", {})
            names = raw_secrets.keys() if isinstance(raw_secrets, dict) else ()
        else:
            names = request_or_payload.secrets.keys()

        for name in names:
            if registry.get(name) is None:
                if exposure_policy == SecretExposurePolicy.SANDBOX_ENV:
                    dest_var = (
                        name
                        if name in ALLOWED_SECRET_ENV_VARS or name.startswith("CODE_AGENT_SECRET_")
                        else f"CODE_AGENT_SECRET_{name.upper()}"
                    )
                else:
                    dest_var = None

                registry.register(
                    RegisteredSecretDefinition(
                        name=name,
                        source=SecretSource.EPHEMERAL,
                        source_key=name,
                        required_scope=scope,
                        exposure_policy=exposure_policy,
                        permitted_egress_hosts=tuple(permitted_egress_hosts),
                        destination_env_var=dest_var,
                    )
                )
        return refs


__all__ = [
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "IngressMigrationAdapter",
    "LegacyIngressTaskRequest",
    "sanitize_legacy_ingress_payload",
]

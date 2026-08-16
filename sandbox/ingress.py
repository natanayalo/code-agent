"""Legacy ingress migration contracts and pre-validation sanitization."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
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


def sanitize_legacy_ingress_payload(payload: Any) -> dict[str, Any]:
    """Sanitize raw transport payload before Pydantic model validation.

    Ensures that any non-dict secrets payload or raw secret values are safely
    scrubbed from the input dictionary so Pydantic ValidationError can never
    capture or echo raw secret material in error contexts.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("Ingress task payload must be a JSON object / dictionary")

    sanitized = dict(payload)
    if "secrets" in sanitized:
        raw_secrets = sanitized["secrets"]
        if isinstance(raw_secrets, Mapping):
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
    def from_raw_payload(cls, raw_payload: Any) -> LegacyIngressTaskRequest:
        """Construct request from raw transport payload after pre-validation sanitization."""
        sanitized = sanitize_legacy_ingress_payload(raw_payload)
        return cls.model_validate(sanitized)

    @model_validator(mode="before")
    @classmethod
    def _scrub_raw_secret_values(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "secrets" in data:
            raw_secrets = data["secrets"]
            if isinstance(raw_secrets, Mapping):
                scrubbed_secrets = {str(k): "[REDACTED_AT_INGRESS]" for k in raw_secrets}
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
        request: LegacyIngressTaskRequest,
        *,
        reject_legacy_secrets: bool = False,
    ) -> tuple[SecretRef, ...]:
        """Extract logical secret names from a sanitized LegacyIngressTaskRequest."""
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
        for name in request.secrets:
            adapted_refs.append(SecretRef(name=name))

        return tuple(adapted_refs)

    @staticmethod
    def _extract_raw_secrets(raw_payload: Mapping[str, Any]) -> dict[str, str]:
        temp_raw_secrets: dict[str, str] = {}
        raw_secrets = raw_payload.get("secrets")
        if isinstance(raw_secrets, Mapping):
            for k, v in raw_secrets.items():
                if isinstance(v, str) and v and v != "[REDACTED_AT_INGRESS]":
                    temp_raw_secrets[str(k)] = v
        return temp_raw_secrets

    @staticmethod
    def _compute_destinations(
        legacy_key: str,
        exposure_policy: SecretExposurePolicy,
    ) -> tuple[str | None, str | None]:
        if exposure_policy == SecretExposurePolicy.SANDBOX_ENV:
            if legacy_key in ALLOWED_SECRET_ENV_VARS:
                dest_env = legacy_key
            elif legacy_key.upper() in ALLOWED_SECRET_ENV_VARS:
                dest_env = legacy_key.upper()
            else:
                clean_env = re.sub(r"[^A-Z0-9_]", "_", legacy_key.upper())
                dest_env = (
                    clean_env
                    if clean_env.startswith("CODE_AGENT_SECRET_")
                    else f"CODE_AGENT_SECRET_{clean_env}"
                )
            return dest_env, None
        if exposure_policy == SecretExposurePolicy.SANDBOX_FILE:
            clean_file = re.sub(r"[^a-zA-Z0-9_.-]", "_", legacy_key)
            return None, f"ephemeral_{clean_file}.secret"
        return None, None

    @classmethod
    def adapt_and_register_ephemeral(
        cls,
        raw_payload: Mapping[str, Any],
        *,
        registry: SecretRegistry,
        ephemeral_store: EphemeralSecretStore,
        scope: SecretScope = SecretScope.CUSTOM,
        exposure_policy: SecretExposurePolicy = SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts: Sequence[str] = (),
    ) -> tuple[SecretRef, ...]:
        """Adapt raw payload into ephemeral store entries and register definitions."""
        if not isinstance(raw_payload, Mapping):
            raise ValueError("Ingress task payload must be a JSON object / dictionary")

        temp_raw_secrets = cls._extract_raw_secrets(raw_payload)

        # Pre-sanitize and strictly validate the request model before touching the store
        sanitized_dict = sanitize_legacy_ingress_payload(raw_payload)
        validated_req = LegacyIngressTaskRequest.model_validate(sanitized_dict)

        # Check for conflict between explicit secret_refs and legacy secrets
        ref_names = {ref.name for ref in validated_req.secret_refs}
        conflicts = ref_names.intersection(temp_raw_secrets.keys())
        if conflicts:
            raise ConflictingSecretDeclarationError(
                f"Conflicting secret declarations for keys: {sorted(conflicts)}"
            )

        adapted_refs = list(validated_req.secret_refs)

        # Commit secrets to ephemeral store and register opaque definitions
        for legacy_key, raw_val in temp_raw_secrets.items():
            clean_key = re.sub(r"[^a-zA-Z0-9_]", "_", legacy_key)[:32]
            token = uuid.uuid4().hex[:8]
            opaque_name = f"ephem_{clean_key}_{token}"[:64]

            if registry.get(opaque_name) is not None:
                raise ConflictingSecretDeclarationError(
                    f"Ephemeral secret definition {opaque_name!r} already exists in registry"
                )

            dest_env, dest_mount = cls._compute_destinations(legacy_key, exposure_policy)
            ephemeral_store.store(opaque_name, raw_val)

            registry.register(
                RegisteredSecretDefinition(
                    name=opaque_name,
                    source=SecretSource.EPHEMERAL,
                    source_key=opaque_name,
                    required_scope=scope,
                    exposure_policy=exposure_policy,
                    permitted_egress_hosts=tuple(permitted_egress_hosts),
                    destination_env_var=dest_env,
                    destination_mount_path=dest_mount,
                )
            )
            adapted_refs.append(SecretRef(name=opaque_name))

        return tuple(adapted_refs)


__all__ = [
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "IngressMigrationAdapter",
    "LegacyIngressTaskRequest",
    "sanitize_legacy_ingress_payload",
]

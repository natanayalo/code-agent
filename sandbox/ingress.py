"""Legacy ingress migration contracts and pre-validation sanitization."""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.secrets import (
    CapabilityViolationError,
    EphemeralSecretRecord,
    EphemeralSecretStore,
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretScope,
    SecretSource,
)

_RE_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

RESERVED_LEGACY_SECRET_POLICIES: Final[
    dict[str, tuple[SecretScope, SecretExposurePolicy, str | None]]
] = {
    "github_token": (SecretScope.GIT_PUSH, SecretExposurePolicy.BROKER_ONLY, None),
    "gh_token": (SecretScope.GIT_PUSH, SecretExposurePolicy.BROKER_ONLY, None),
    "openai_api_key": (
        SecretScope.PROVIDER_AUTH,
        SecretExposurePolicy.SANDBOX_ENV,
        "OPENAI_API_KEY",
    ),
    "openai_key": (SecretScope.PROVIDER_AUTH, SecretExposurePolicy.SANDBOX_ENV, "OPENAI_API_KEY"),
    "gemini_api_key": (
        SecretScope.PROVIDER_AUTH,
        SecretExposurePolicy.SANDBOX_ENV,
        "GEMINI_API_KEY",
    ),
    "openrouter_api_key": (
        SecretScope.PROVIDER_AUTH,
        SecretExposurePolicy.SANDBOX_ENV,
        "OPENROUTER_API_KEY",
    ),
}


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
    def _compute_destinations_and_policy(
        legacy_key: str,
        default_scope: SecretScope,
        default_exposure: SecretExposurePolicy,
    ) -> tuple[SecretScope, SecretExposurePolicy, str | None, str | None]:
        norm = legacy_key.lower().replace("-", "_")
        if norm in RESERVED_LEGACY_SECRET_POLICIES:
            res_scope, res_exposure, res_dest = RESERVED_LEGACY_SECRET_POLICIES[norm]
            if res_exposure == SecretExposurePolicy.BROKER_ONLY:
                return res_scope, res_exposure, None, None
            if res_exposure == SecretExposurePolicy.SANDBOX_ENV:
                return res_scope, res_exposure, res_dest, None

        if default_exposure == SecretExposurePolicy.SANDBOX_ENV:
            clean_env = re.sub(r"[^A-Z0-9_]", "_", legacy_key.upper())[:44]
            dest_env = (
                clean_env
                if clean_env.startswith("CODE_AGENT_SECRET_")
                else f"CODE_AGENT_SECRET_{clean_env}"
            )
            return default_scope, default_exposure, dest_env, None
        if default_exposure == SecretExposurePolicy.SANDBOX_FILE:
            clean_file = re.sub(r"[^a-zA-Z0-9_.-]", "_", legacy_key)[:48]
            return default_scope, default_exposure, None, f"ephemeral_{clean_file}.secret"
        return default_scope, default_exposure, None, None

    @classmethod
    def adapt_and_register_ephemeral(
        cls,
        raw_payload: Mapping[str, Any],
        *,
        registry: SecretRegistry,
        ephemeral_store: EphemeralSecretStore,
        task_id: str = "default_task",
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

        ref_names = {ref.name for ref in validated_req.secret_refs}
        conflicts = ref_names.intersection(temp_raw_secrets.keys())
        if conflicts:
            raise ConflictingSecretDeclarationError(
                f"Conflicting secret declarations for keys: {sorted(conflicts)}"
            )

        definitions_to_register: list[RegisteredSecretDefinition] = []
        records_to_commit: list[EphemeralSecretRecord] = []
        adapted_refs = list(validated_req.secret_refs)

        # Pre-build and validate all definitions and records in memory
        for legacy_key, raw_val in temp_raw_secrets.items():
            clean_key = re.sub(r"[^a-zA-Z0-9_]", "_", legacy_key)[:24]
            token = secrets.token_hex(16)
            opaque_name = f"ephem_{clean_key}_{token}"[:64]

            if registry.get(opaque_name, task_id=task_id) is not None:
                raise ConflictingSecretDeclarationError(
                    f"Ephemeral secret definition {opaque_name!r} already exists in registry"
                )

            sec_scope, sec_exposure, dest_env, dest_mount = cls._compute_destinations_and_policy(
                legacy_key, default_scope=scope, default_exposure=exposure_policy
            )

            definition = RegisteredSecretDefinition(
                name=opaque_name,
                source=SecretSource.EPHEMERAL,
                source_key=opaque_name,
                required_scope=sec_scope,
                exposure_policy=sec_exposure,
                permitted_egress_hosts=tuple(permitted_egress_hosts),
                destination_env_var=dest_env,
                destination_mount_path=dest_mount,
            )
            record = EphemeralSecretRecord(
                handle_id=opaque_name,
                task_id=task_id,
                value=raw_val,
                required_scope=sec_scope,
                exposure_policy=sec_exposure,
                permitted_egress_hosts=tuple(permitted_egress_hosts),
                destination_env_var=dest_env,
                destination_mount_path=dest_mount,
            )
            definitions_to_register.append(definition)
            records_to_commit.append(record)
            adapted_refs.append(SecretRef(name=opaque_name))

        # Commit all records and definitions transactionally
        for record, definition in zip(records_to_commit, definitions_to_register):
            ephemeral_store.store_record(record)
            registry.register(definition)

        return tuple(adapted_refs)


__all__ = [
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "IngressMigrationAdapter",
    "LegacyIngressTaskRequest",
    "sanitize_legacy_ingress_payload",
]

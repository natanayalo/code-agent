"""Legacy ingress migration contracts and pre-validation sanitization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sandbox.secrets import (
    CapabilityViolationError,
    DeprecatedLegacySecretsError,
    SecretRef,
)


class ConflictingSecretDeclarationError(CapabilityViolationError):
    """Raised when secret declarations declare conflicting keys."""


def sanitize_legacy_ingress_payload(payload: Any) -> dict[str, Any]:
    """Sanitize raw transport payload before validation or error reporting.

    Ensures that any non-dict secrets payload or raw secret values are safely
    scrubbed from the input dictionary so ValidationError or logs can never
    capture or echo raw secret material in error contexts.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("Ingress task payload must be a JSON object / dictionary")

    sanitized = dict(payload)
    if "secrets" in sanitized:
        raw_secrets = sanitized["secrets"]
        if isinstance(raw_secrets, Mapping):
            # Scrub values to placeholders, preserving keys for safe inspection
            sanitized["secrets"] = {str(k): "[REDACTED_AT_INGRESS]" for k in raw_secrets}
        else:
            # Replace malformed non-dict secrets with empty dict and reject
            sanitized["secrets"] = {}
            raise ValueError(
                "Invalid secrets payload: secrets must be a dictionary of key-value pairs"
            )
    return sanitized


class IngressMigrationAdapter:
    """Validates ingress secret declarations and rejects legacy raw secrets fail-closed."""

    @classmethod
    def adapt(
        cls,
        *,
        secret_refs: Sequence[SecretRef] = (),
        secrets: Mapping[str, str] | None = None,
        reject_legacy_secrets: bool = True,
    ) -> tuple[SecretRef, ...]:
        """Validate references and reject legacy raw secrets fail-closed."""
        if secrets:
            raise DeprecatedLegacySecretsError(
                "Legacy raw secrets are no longer accepted. Use secret_refs instead."
            )
        return tuple(secret_refs)


__all__ = [
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "IngressMigrationAdapter",
    "sanitize_legacy_ingress_payload",
]

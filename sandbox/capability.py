"""Sandbox capability grants, resource limits, and least-privilege contracts."""

from __future__ import annotations

import enum
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.secrets import (
    ALLOWED_SECRET_ENV_VARS,
    BrokerOnlySecretExposureError,
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    IngressMigrationAdapter,
    LegacyIngressTaskRequest,
    MissingSecretScopeError,
    RegisteredSecretDefinition,
    ResolvedSecret,
    SecretExposurePolicy,
    SecretNotFoundError,
    SecretRef,
    SecretRegistry,
    SecretResolutionError,
    SecretResolver,
    SecretScope,
    SecretSource,
    UnauthorizedSecretError,
    normalize_fqdn,
)

DANGEROUS_SHELL_TOOLS: Final[frozenset[str]] = frozenset(
    {"execute_shell", "run_shell_command", "bash", "sh"}
)
DEFAULT_AUTOMATED_PUBLICATION_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "execute_git",
        "git_push",
        "create_pr",
        "upload_artifact",
        "publish_release",
        "send_external_message",
    }
)


class CapabilityViolationError(RuntimeError):
    """Raised when a capability grant or execution request violates security policy."""


class ToolCapabilityTag(enum.StrEnum):
    """Semantic capability tags for tools."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGEROUS_SHELL = "dangerous_shell"
    AUTOMATED_EXTERNAL_PUBLICATION = "automated_external_publication"
    NETWORK_EGRESS = "network_egress"


class NetworkEgressPolicy(enum.StrEnum):
    """Network egress policy for containerized sandbox execution."""

    DISABLED = "disabled"
    PUBLIC_HTTPS_PROXY = "public_https_proxy"
    ALLOWLISTED_HOSTS = "allowlisted_hosts"


class FileSystemAccessPolicy(enum.StrEnum):
    """Filesystem access mode for sandbox execution."""

    SCRATCH_ONLY = "scratch_only"
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


def parse_memory_bytes(value: str | int) -> int:
    """Parse human-readable memory string or integer into exact bytes with bounds checking."""
    if isinstance(value, int):
        bytes_val = value
    elif isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            raise ValueError("Memory limit string cannot be empty")
        units = {
            "k": 1024,
            "kb": 1000,
            "kib": 1024,
            "m": 1024 * 1024,
            "mb": 1000 * 1000,
            "mib": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
            "gb": 1000 * 1000 * 1000,
            "gib": 1024 * 1024 * 1024,
        }
        matched_unit = None
        for unit_suffix in sorted(units.keys(), key=len, reverse=True):
            if cleaned.endswith(unit_suffix):
                matched_unit = unit_suffix
                break

        if matched_unit:
            num_part = cleaned[: -len(matched_unit)].strip()
            try:
                num = float(num_part)
            except ValueError as err:
                raise ValueError(f"Invalid numeric memory value: {num_part!r}") from err
            if not math.isfinite(num) or num <= 0:
                raise ValueError(f"Memory limit must be positive and finite: {value!r}")
            bytes_val = int(num * units[matched_unit])
        else:
            try:
                bytes_val = int(cleaned)
            except ValueError as err:
                raise ValueError(f"Invalid memory limit string format: {value!r}") from err
    else:
        raise ValueError(f"Unsupported memory limit type: {type(value)}")

    min_bytes = 64 * 1024 * 1024  # 64MiB
    max_bytes = 8 * 1024 * 1024 * 1024  # 8GiB
    if not (min_bytes <= bytes_val <= max_bytes):
        raise ValueError(
            f"Memory limit {bytes_val} bytes out of bounds ({min_bytes}B - {max_bytes}B)"
        )
    return bytes_val


class ResourceLimits(BaseModel):
    """Immutable resource constraints for containerized sandbox execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_limit: float = 1.0
    memory_bytes: int = 1073741824  # 1GiB
    pids_limit: int = 256
    timeout_seconds: int = 600

    @field_validator("cpu_limit")
    @classmethod
    def _validate_cpu(cls, v: float) -> float:
        if not math.isfinite(v) or not (0.1 <= v <= 4.0):
            raise ValueError(f"cpu_limit must be a finite float between 0.1 and 4.0, got {v!r}")
        return float(v)

    @field_validator("memory_bytes", mode="before")
    @classmethod
    def _validate_memory(cls, v: Any) -> int:
        return parse_memory_bytes(v)

    @field_validator("pids_limit")
    @classmethod
    def _validate_pids(cls, v: int) -> int:
        if not (16 <= v <= 1024):
            raise ValueError(f"pids_limit must be between 16 and 1024, got {v!r}")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, v: int) -> int:
        if not (1 <= v <= 3600):
            raise ValueError(f"timeout_seconds must be between 1 and 3600, got {v!r}")
        return v


class SandboxCapabilityGrant(BaseModel):
    """Immutable, broker-issued capability grant governing sandbox execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    network: NetworkEgressPolicy = NetworkEgressPolicy.DISABLED
    filesystem: FileSystemAccessPolicy = FileSystemAccessPolicy.SCRATCH_ONLY
    allow_dangerous_shell: bool = False
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ("/workspace/.git/hooks", "/workspace/.git/config")
    allowed_tools: tuple[str, ...] = ()
    allowed_secret_refs: tuple[str, ...] = ()
    granted_secret_scopes: frozenset[SecretScope] = frozenset()
    allowed_egress_hosts: tuple[str, ...] = ()
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @field_validator("allowed_egress_hosts")
    @classmethod
    def _validate_egress_hosts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_fqdn(h) for h in v)

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> SandboxCapabilityGrant:
        if self.network == NetworkEgressPolicy.DISABLED:
            if self.allowed_egress_hosts:
                raise ValueError("DISABLED network cannot specify allowed_egress_hosts")
        elif self.network == NetworkEgressPolicy.ALLOWLISTED_HOSTS:
            if not self.allowed_egress_hosts:
                raise ValueError(
                    "ALLOWLISTED_HOSTS network requires non-empty allowed_egress_hosts"
                )
        return self


class CapabilityGrantFactory:
    """Server-side deterministic factory that derives and issues SandboxCapabilityGrant."""

    @classmethod
    def create_grant(
        cls,
        *,
        network: NetworkEgressPolicy = NetworkEgressPolicy.DISABLED,
        filesystem: FileSystemAccessPolicy = FileSystemAccessPolicy.SCRATCH_ONLY,
        allowed_tools: Sequence[str] = (),
        allowed_secret_defs: Sequence[RegisteredSecretDefinition] = (),
        granted_secret_scopes: Iterable[SecretScope] = (),
        allowed_egress_hosts: Sequence[str] = (),
        allowed_paths: Sequence[str] = (),
        denied_paths: Sequence[str] = ("/workspace/.git/hooks", "/workspace/.git/config"),
        resource_limits: ResourceLimits | None = None,
        publication_tools: frozenset[str] = DEFAULT_AUTOMATED_PUBLICATION_TOOLS,
    ) -> SandboxCapabilityGrant:
        """Create and validate a broker-issued capability grant."""
        tools_tuple = tuple(allowed_tools)
        scopes_set = frozenset(granted_secret_scopes)
        secret_names_tuple = tuple(s.name for s in allowed_secret_defs)
        normalized_hosts = tuple(normalize_fqdn(h) for h in allowed_egress_hosts)

        # 1. Dangerous shell bidirectional check
        has_dangerous_tool = bool(set(tools_tuple) & DANGEROUS_SHELL_TOOLS)
        allow_dangerous_shell = has_dangerous_tool

        # 2. Separate sandbox-exposed vs broker-only secrets
        sandbox_secrets = [
            s
            for s in allowed_secret_defs
            if s.exposure_policy
            in (SecretExposurePolicy.SANDBOX_ENV, SecretExposurePolicy.SANDBOX_FILE)
        ]

        # 3. Publication capability coupling check
        if sandbox_secrets:
            has_pub_tool = bool(set(tools_tuple) & publication_tools)
            if has_pub_tool:
                raise CapabilityViolationError(
                    "Sandbox-exposed secrets cannot be combined with "
                    "automated external publication capabilities"
                )

        # 4. Network and secret audience validation
        if sandbox_secrets:
            if network == NetworkEgressPolicy.PUBLIC_HTTPS_PROXY:
                raise CapabilityViolationError(
                    "PUBLIC_HTTPS_PROXY is forbidden when sandbox secrets are granted"
                )
            elif network == NetworkEgressPolicy.ALLOWLISTED_HOSTS:
                if not normalized_hosts:
                    raise CapabilityViolationError(
                        "ALLOWLISTED_HOSTS requires non-empty allowed_egress_hosts"
                    )
                permitted_sets = [set(s.permitted_egress_hosts) for s in sandbox_secrets]
                intersection = set.intersection(*permitted_sets) if permitted_sets else set()
                if not set(normalized_hosts).issubset(intersection):
                    raise CapabilityViolationError(
                        f"allowed_egress_hosts {normalized_hosts} exceeds "
                        f"sandbox secret audience intersection {intersection}"
                    )
            elif network == NetworkEgressPolicy.DISABLED:
                if normalized_hosts:
                    raise CapabilityViolationError(
                        "DISABLED network cannot specify allowed_egress_hosts"
                    )

        return SandboxCapabilityGrant(
            network=network,
            filesystem=filesystem,
            allow_dangerous_shell=allow_dangerous_shell,
            allowed_paths=tuple(allowed_paths),
            denied_paths=tuple(denied_paths),
            allowed_tools=tools_tuple,
            allowed_secret_refs=secret_names_tuple,
            granted_secret_scopes=scopes_set,
            allowed_egress_hosts=normalized_hosts,
            resource_limits=resource_limits or ResourceLimits(),
        )


__all__ = [
    "ALLOWED_SECRET_ENV_VARS",
    "DANGEROUS_SHELL_TOOLS",
    "DEFAULT_AUTOMATED_PUBLICATION_TOOLS",
    "BrokerOnlySecretExposureError",
    "CapabilityGrantFactory",
    "CapabilityViolationError",
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "FileSystemAccessPolicy",
    "IngressMigrationAdapter",
    "LegacyIngressTaskRequest",
    "MissingSecretScopeError",
    "NetworkEgressPolicy",
    "RegisteredSecretDefinition",
    "ResourceLimits",
    "ResolvedSecret",
    "SandboxCapabilityGrant",
    "SecretExposurePolicy",
    "SecretNotFoundError",
    "SecretRef",
    "SecretRegistry",
    "SecretResolutionError",
    "SecretResolver",
    "SecretScope",
    "SecretSource",
    "ToolCapabilityTag",
    "UnauthorizedSecretError",
    "normalize_fqdn",
    "parse_memory_bytes",
]

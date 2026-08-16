"""Sandbox capability grants, resource limits, and least-privilege contracts."""

from __future__ import annotations

import enum
import math
import posixpath
from collections.abc import Iterable, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.ingress import (
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    IngressMigrationAdapter,
    LegacyIngressTaskRequest,
    sanitize_legacy_ingress_payload,
)
from sandbox.secrets import (
    ALLOWED_SECRET_ENV_VARS,
    DEFAULT_SECRET_REGISTRY,
    BrokerOnlySecretExposureError,
    CapabilityViolationError,
    EphemeralSecretHandle,
    EphemeralSecretStore,
    InMemoryEphemeralSecretStore,
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
from tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    ToolCapabilityTag,
    ToolRegistry,
    UnknownToolError,
)

MANDATORY_DENIED_PATHS: Final[tuple[str, ...]] = (
    "/workspace/.git/hooks",
    "/workspace/.git/config",
)
SCRATCH_PATH_PREFIX: Final[str] = "/workspace/.code-agent/scratch"


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
    """Parse human-readable memory string or integer into exact bytes with bounds checking.

    Supports integral numbers as well as fractional units (e.g. '1.5g' -> 1610612736 bytes).
    """
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


def _validate_scratch_path(path: str) -> None:
    """Validate that path is a normalized POSIX subpath of SCRATCH_PATH_PREFIX with no traversal."""
    if not path or not isinstance(path, str):
        raise ValueError(f"Invalid path: {path!r}")

    parts = path.split("/")
    if ".." in parts or "." in parts:
        raise ValueError(f"Relative path traversal components ('.', '..') forbidden: {path!r}")

    normalized = posixpath.normpath(path)
    if ".." in normalized.split("/"):
        raise ValueError(f"Path traversal detected: {path!r}")

    if not (normalized == SCRATCH_PATH_PREFIX or normalized.startswith(SCRATCH_PATH_PREFIX + "/")):
        raise ValueError(
            f"SCRATCH_ONLY filesystem policy only permits paths under "
            f"{SCRATCH_PATH_PREFIX}, got {path!r}"
        )


class SandboxCapabilityGrant(BaseModel):
    """Immutable, broker-issued capability grant governing sandbox execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    network: NetworkEgressPolicy = NetworkEgressPolicy.DISABLED
    filesystem: FileSystemAccessPolicy = FileSystemAccessPolicy.SCRATCH_ONLY
    allow_dangerous_shell: bool = False
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = MANDATORY_DENIED_PATHS
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
        # 1. Mandatory denied paths invariant
        if not set(MANDATORY_DENIED_PATHS).issubset(set(self.denied_paths)):
            raise ValueError(
                f"denied_paths must include mandatory denied paths {MANDATORY_DENIED_PATHS}"
            )

        # 2. Filesystem path invariants
        if self.filesystem == FileSystemAccessPolicy.SCRATCH_ONLY:
            for path in self.allowed_paths:
                _validate_scratch_path(path)

        # 3. Network egress invariants
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

    def __init__(
        self,
        secret_registry: SecretRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._secret_registry = (
            secret_registry if secret_registry is not None else DEFAULT_SECRET_REGISTRY
        )
        self._tool_registry = tool_registry if tool_registry is not None else DEFAULT_TOOL_REGISTRY

    def _resolve_and_validate_secrets(
        self,
        allowed_secret_refs: Sequence[SecretRef | str],
        scopes_set: frozenset[SecretScope],
    ) -> tuple[tuple[str, ...], list[RegisteredSecretDefinition]]:
        ref_names: list[str] = []
        canonical_defs: list[RegisteredSecretDefinition] = []
        for ref in allowed_secret_refs:
            name = ref.name if isinstance(ref, SecretRef) else ref
            if not isinstance(name, str) or not name:
                raise CapabilityViolationError(f"Invalid secret reference: {ref!r}")
            try:
                sec_def = self._secret_registry.require(name)
            except (SecretNotFoundError, LookupError) as err:
                raise CapabilityViolationError(
                    f"Secret {name!r} is not registered in authoritative SecretRegistry"
                ) from err

            if sec_def.required_scope not in scopes_set:
                raise CapabilityViolationError(
                    f"Secret {name!r} requires scope {sec_def.required_scope.value!r}, "
                    f"which is not present in granted_secret_scopes: "
                    f"{sorted(s.value for s in scopes_set)}"
                )

            ref_names.append(name)
            canonical_defs.append(sec_def)

        return tuple(ref_names), canonical_defs

    def _validate_tools_and_dangerous_shell(
        self,
        tools_tuple: tuple[str, ...],
        allow_dangerous_shell: bool,
    ) -> list[Any]:
        registered_tools = []
        for tool_name in tools_tuple:
            try:
                registered_tools.append(self._tool_registry.require_tool(tool_name))
            except (UnknownToolError, LookupError) as err:
                raise CapabilityViolationError(
                    f"Tool {tool_name!r} is not registered in ToolRegistry"
                ) from err

        has_dangerous_tool = any(
            ToolCapabilityTag.DANGEROUS_SHELL in tool.capability_tags for tool in registered_tools
        )
        if has_dangerous_tool and not allow_dangerous_shell:
            raise CapabilityViolationError(
                "Dangerous shell tool requested without explicit allow_dangerous_shell=True grant"
            )
        if allow_dangerous_shell and not has_dangerous_tool:
            raise CapabilityViolationError(
                "allow_dangerous_shell=True granted without dangerous shell tools in allowed_tools"
            )
        return registered_tools

    @classmethod
    def _validate_publication_coupling(
        cls,
        sandbox_secrets: list[RegisteredSecretDefinition],
        registered_tools: list[Any],
    ) -> None:
        if sandbox_secrets:
            has_pub_tool = any(
                ToolCapabilityTag.AUTOMATED_EXTERNAL_PUBLICATION in tool.capability_tags
                for tool in registered_tools
            )
            if has_pub_tool:
                raise CapabilityViolationError(
                    "Sandbox-exposed secrets cannot be combined with "
                    "automated external publication capabilities"
                )

    @classmethod
    def _validate_filesystem_paths(
        cls,
        filesystem: FileSystemAccessPolicy,
        allowed_paths: Sequence[str],
        denied_paths: Sequence[str],
    ) -> None:
        if not set(MANDATORY_DENIED_PATHS).issubset(set(denied_paths)):
            raise CapabilityViolationError(
                f"denied_paths must include mandatory denied paths {MANDATORY_DENIED_PATHS}"
            )
        if filesystem == FileSystemAccessPolicy.SCRATCH_ONLY:
            for p in allowed_paths:
                try:
                    _validate_scratch_path(p)
                except ValueError as err:
                    raise CapabilityViolationError(str(err)) from err

    @classmethod
    def _validate_network_and_audience(
        cls,
        network: NetworkEgressPolicy,
        sandbox_secrets: list[RegisteredSecretDefinition],
        normalized_hosts: tuple[str, ...],
    ) -> None:
        if not sandbox_secrets:
            return

        if network == NetworkEgressPolicy.PUBLIC_HTTPS_PROXY:
            raise CapabilityViolationError(
                "PUBLIC_HTTPS_PROXY is forbidden when sandbox secrets are granted"
            )
        if network == NetworkEgressPolicy.ALLOWLISTED_HOSTS:
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

    def create_grant(
        self,
        *,
        network: NetworkEgressPolicy = NetworkEgressPolicy.DISABLED,
        filesystem: FileSystemAccessPolicy = FileSystemAccessPolicy.SCRATCH_ONLY,
        allow_dangerous_shell: bool = False,
        allowed_paths: Sequence[str] = (),
        allowed_tools: Sequence[str] = (),
        allowed_secret_refs: Sequence[SecretRef | str] = (),
        granted_secret_scopes: Iterable[SecretScope] = (),
        allowed_egress_hosts: Sequence[str] = (),
        denied_paths: Sequence[str] = MANDATORY_DENIED_PATHS,
        resource_limits: ResourceLimits | None = None,
    ) -> SandboxCapabilityGrant:
        """Create and validate a broker-issued capability grant."""
        tools_tuple = tuple(allowed_tools)
        scopes_set = frozenset(granted_secret_scopes)
        normalized_hosts = tuple(normalize_fqdn(h) for h in allowed_egress_hosts)

        ref_names, canonical_defs = self._resolve_and_validate_secrets(
            allowed_secret_refs, scopes_set
        )
        registered_tools = self._validate_tools_and_dangerous_shell(
            tools_tuple, allow_dangerous_shell
        )

        sandbox_secrets = [
            s
            for s in canonical_defs
            if s.exposure_policy
            in (SecretExposurePolicy.SANDBOX_ENV, SecretExposurePolicy.SANDBOX_FILE)
        ]

        self._validate_publication_coupling(sandbox_secrets, registered_tools)
        self._validate_filesystem_paths(filesystem, allowed_paths, denied_paths)
        self._validate_network_and_audience(network, sandbox_secrets, normalized_hosts)

        return SandboxCapabilityGrant(
            network=network,
            filesystem=filesystem,
            allow_dangerous_shell=allow_dangerous_shell,
            allowed_paths=tuple(allowed_paths),
            denied_paths=tuple(denied_paths),
            allowed_tools=tools_tuple,
            allowed_secret_refs=ref_names,
            granted_secret_scopes=scopes_set,
            allowed_egress_hosts=normalized_hosts,
            resource_limits=resource_limits or ResourceLimits(),
        )


def validate_grant_for_execution(
    grant: SandboxCapabilityGrant,
    *,
    secret_registry: SecretRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
) -> None:
    """Validate a SandboxCapabilityGrant at consumption time against authoritative registries.

    Fails closed with CapabilityViolationError if the grant violates security invariants,
    protecting against direct manual instantiation or deserialization of forbidden grants.
    """
    factory = CapabilityGrantFactory(secret_registry=secret_registry, tool_registry=tool_registry)
    registered_tools = factory._validate_tools_and_dangerous_shell(
        grant.allowed_tools, grant.allow_dangerous_shell
    )
    _, canonical_defs = factory._resolve_and_validate_secrets(
        grant.allowed_secret_refs, grant.granted_secret_scopes
    )
    sandbox_secrets = [
        s
        for s in canonical_defs
        if s.exposure_policy
        in (SecretExposurePolicy.SANDBOX_ENV, SecretExposurePolicy.SANDBOX_FILE)
    ]
    factory._validate_publication_coupling(sandbox_secrets, registered_tools)
    factory._validate_filesystem_paths(grant.filesystem, grant.allowed_paths, grant.denied_paths)
    factory._validate_network_and_audience(
        grant.network, sandbox_secrets, grant.allowed_egress_hosts
    )


__all__ = [
    "ALLOWED_SECRET_ENV_VARS",
    "DEFAULT_SECRET_REGISTRY",
    "MANDATORY_DENIED_PATHS",
    "SCRATCH_PATH_PREFIX",
    "BrokerOnlySecretExposureError",
    "CapabilityGrantFactory",
    "CapabilityViolationError",
    "ConflictingSecretDeclarationError",
    "DeprecatedLegacySecretsError",
    "EphemeralSecretHandle",
    "EphemeralSecretStore",
    "FileSystemAccessPolicy",
    "InMemoryEphemeralSecretStore",
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
    "sanitize_legacy_ingress_payload",
    "validate_grant_for_execution",
]

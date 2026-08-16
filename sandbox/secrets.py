"""Sandbox secret references, registered definitions, and resolution engine."""

from __future__ import annotations

import enum
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.redact import SecretRedactor
from tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    ToolCapabilityTag,
    ToolRegistry,
)

ALLOWED_SECRET_ENV_VARS: Final[frozenset[str]] = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"}
)
_SECRET_ENV_VAR_PREFIX: Final[str] = "CODE_AGENT_SECRET_"
_RE_SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_RE_SAFE_FILENAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_RE_SAFE_ENV_VAR: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_RE_FQDN: Final[re.Pattern[str]] = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
)


class CapabilityViolationError(RuntimeError):
    """Raised when a capability grant or execution request violates security policy."""


class SecretResolutionError(CapabilityViolationError):
    """Base exception for secret resolution failures."""


class UnauthorizedSecretError(SecretResolutionError):
    """Raised when access to a secret is not authorized by the capability grant."""


class MissingSecretScopeError(SecretResolutionError):
    """Raised when a capability grant lacks the scope required by a secret."""


class SecretNotFoundError(SecretResolutionError):
    """Raised when a requested secret is not found in the registry or source."""


class BrokerOnlySecretExposureError(SecretResolutionError):
    """Raised when attempting to resolve a broker-only secret for sandbox injection."""


class SecretSource(enum.StrEnum):
    """Supported backing sources for registered secrets."""

    ENV = "env"
    SECRET_STORE = "secret_store"
    FILE = "file"
    EPHEMERAL = "ephemeral"


class EphemeralSecretHandle(BaseModel):
    """Opaque reference to an ephemeral secret stored outside durable workflow history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handle_id: str = Field(min_length=1)


class EphemeralSecretStore:
    """Storage interface for ephemeral legacy secrets outside durable workflow history.

    Provides the base interface and in-memory reference implementation for
    single-process execution and tests. In distributed production (M28.5A.2), a shared
    out-of-history encrypted backend (e.g. Redis TTL or Vault KV) implements this contract.
    """

    def __init__(self, initial_secrets: Mapping[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial_secrets) if initial_secrets is not None else {}

    def get(self, handle_or_key: str) -> str | None:
        """Retrieve secret material for an opaque handle or key."""
        return self._store.get(handle_or_key)

    def store(
        self,
        key: str,
        value: str,
        *,
        ttl_seconds: int = 3600,
    ) -> str:
        """Store secret material under a key/handle and return the handle identifier."""
        self._store[key] = value
        return key

    def has(self, handle_or_key: str) -> bool:
        """Check if an ephemeral secret exists in the store."""
        return handle_or_key in self._store

    def remove(self, handle_or_key: str) -> None:
        """Delete an ephemeral secret from the store."""
        self._store.pop(handle_or_key, None)

    def clear(self) -> None:
        """Clear all stored ephemeral secrets."""
        self._store.clear()

    def __contains__(self, handle_or_key: str) -> bool:
        return handle_or_key in self._store

    def __len__(self) -> int:
        return len(self._store)


class InMemoryEphemeralSecretStore(EphemeralSecretStore):
    """In-memory ephemeral secret store implementation for testing and local runtime."""


class SecretScope(enum.StrEnum):
    """Privilege scopes required to access a registered secret."""

    PROVIDER_AUTH = "provider_auth"
    GIT_PUSH = "git_push"
    API_INGRESS = "api_ingress"
    CUSTOM = "custom"


class SecretExposurePolicy(enum.StrEnum):
    """Execution boundary exposure policy for a registered secret."""

    BROKER_ONLY = "broker_only"
    SANDBOX_ENV = "sandbox_env"
    SANDBOX_FILE = "sandbox_file"


def normalize_fqdn(host: str) -> str:
    """Normalize and validate a fully-qualified domain name (FQDN)."""
    stripped = host.strip().rstrip(".").lower()
    try:
        ascii_host = stripped.encode("idna").decode("ascii")
    except Exception as err:
        raise ValueError(f"Invalid IDNA hostname: {host!r}") from err

    if not _RE_FQDN.match(ascii_host):
        raise ValueError(f"Invalid FQDN format: {host!r}")
    return ascii_host


class SecretRef(BaseModel):
    """Caller-supplied reference to a registered secret by logical name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    metadata: tuple[tuple[str, str], ...] = ()

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _RE_SAFE_NAME.match(v):
            raise ValueError(f"Invalid SecretRef name: {v!r}")
        return v

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        if len(v) > 8:
            raise ValueError("SecretRef metadata cannot exceed 8 key-value entries")
        seen_keys: set[str] = set()
        for key, val in v:
            if not _RE_SAFE_NAME.match(key):
                raise ValueError(f"Invalid metadata key: {key!r}")
            if key in seen_keys:
                raise ValueError(f"Duplicate metadata key: {key!r}")
            seen_keys.add(key)
            if len(val) > 128 or any(ord(c) < 32 for c in val):
                raise ValueError(f"Invalid metadata value for key {key!r}")
        return v


class RegisteredSecretDefinition(BaseModel):
    """Broker-owned, authoritative definition of a secret."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1)
    source: SecretSource
    source_key: str = Field(min_length=1)
    required_scope: SecretScope
    exposure_policy: SecretExposurePolicy = SecretExposurePolicy.BROKER_ONLY
    permitted_egress_hosts: tuple[str, ...] = ()
    destination_env_var: str | None = None
    destination_mount_name: str | None = Field(default=None, alias="destination_mount_path")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _RE_SAFE_NAME.match(v):
            raise ValueError(f"Invalid secret definition name: {v!r}")
        return v

    @field_validator("permitted_egress_hosts")
    @classmethod
    def _validate_hosts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(normalize_fqdn(h) for h in v)

    @field_validator("destination_env_var")
    @classmethod
    def _validate_env_var(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _RE_SAFE_ENV_VAR.match(v):
            raise ValueError(f"Invalid destination environment variable name: {v!r}")
        if not (v in ALLOWED_SECRET_ENV_VARS or v.startswith(_SECRET_ENV_VAR_PREFIX)):
            allowed_str = sorted(ALLOWED_SECRET_ENV_VARS)
            raise ValueError(
                f"destination_env_var must be in {allowed_str} "
                f"or prefixed with '{_SECRET_ENV_VAR_PREFIX}', got {v!r}"
            )
        return v

    @field_validator("destination_mount_name", mode="before")
    @classmethod
    def _validate_mount_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if isinstance(v, str) and v.startswith("/run/secrets/code-agent/"):
            v = v.removeprefix("/run/secrets/code-agent/")
        if (
            not isinstance(v, str)
            or not _RE_SAFE_FILENAME.match(v)
            or ".." in v
            or v.startswith(("/", "."))
        ):
            raise ValueError(f"Invalid destination mount path safe name: {v!r}")
        return v

    @property
    def destination_mount_path(self) -> str | None:
        if self.destination_mount_name is None:
            return None
        return f"/run/secrets/code-agent/{self.destination_mount_name}"

    @model_validator(mode="after")
    def _validate_destination_consistency(self) -> RegisteredSecretDefinition:
        if self.source == SecretSource.FILE:
            if not self.source_key or self.source_key.startswith("/"):
                raise ValueError("FILE secret source_key must be a relative path")
            parts = self.source_key.split("/")
            if ".." in parts or "." in parts or "" in parts:
                raise ValueError(
                    f"FILE secret source_key cannot contain traversal components: "
                    f"{self.source_key!r}"
                )
        if self.exposure_policy == SecretExposurePolicy.BROKER_ONLY:
            if self.destination_env_var is not None or self.destination_mount_name is not None:
                raise ValueError(
                    "BROKER_ONLY secrets cannot specify sandbox injection destinations"
                )
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_ENV:
            if self.destination_env_var is None:
                raise ValueError("SANDBOX_ENV secrets must specify destination_env_var")
            if self.destination_mount_name is not None:
                raise ValueError("SANDBOX_ENV secrets cannot specify destination_mount_path")
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_FILE:
            if self.destination_mount_name is None:
                raise ValueError("SANDBOX_FILE secrets must specify destination_mount_path")
            if self.destination_env_var is not None:
                raise ValueError("SANDBOX_FILE secrets cannot specify destination_env_var")
        return self


class ResolvedSecret:
    """Slotted, non-serializable container for resolved secret values."""

    __slots__ = ("_name", "_scope", "_destination_env_var", "_destination_mount_path", "_value")

    def __init__(
        self,
        *,
        name: str,
        scope: SecretScope,
        value: str,
        destination_env_var: str | None = None,
        destination_mount_path: str | None = None,
    ) -> None:
        self._name = name
        self._scope = scope
        self._destination_env_var = destination_env_var
        self._destination_mount_path = destination_mount_path
        self._value = value

    @property
    def name(self) -> str:
        return self._name

    @property
    def scope(self) -> SecretScope:
        return self._scope

    @property
    def destination_env_var(self) -> str | None:
        return self._destination_env_var

    @property
    def destination_mount_path(self) -> str | None:
        return self._destination_mount_path

    def reveal_secret_value(self) -> str:
        """Scoped accessor for direct injection only."""
        return self._value

    def __repr__(self) -> str:
        return f"ResolvedSecret(name={self._name!r}, scope={self._scope.value!r}, redacted=True)"

    def __str__(self) -> str:
        return self.__repr__()


class SecretRegistry:
    """Authoritative server-side registry of registered secret definitions."""

    def __init__(self, definitions: Sequence[RegisteredSecretDefinition] = ()) -> None:
        self._definitions: dict[str, RegisteredSecretDefinition] = {}
        for d in definitions:
            self.register(d)

    def register(self, definition: RegisteredSecretDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Secret {definition.name!r} already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> RegisteredSecretDefinition | None:
        return self._definitions.get(name)

    def require(self, name: str) -> RegisteredSecretDefinition:
        definition = self.get(name)
        if definition is None:
            raise SecretNotFoundError(f"Secret {name!r} not found in registry")
        return definition

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, name: str) -> bool:
        return name in self._definitions

    def __iter__(self) -> Iterator[RegisteredSecretDefinition]:
        return iter(self._definitions.values())


DEFAULT_SECRET_REGISTRY: Final[SecretRegistry] = SecretRegistry()


class SecretResolver:
    """Resolves secret definitions into ResolvedSecret instances under a capability grant."""

    def __init__(
        self,
        registry: SecretRegistry,
        *,
        env: Mapping[str, str] | None = None,
        secret_store: Mapping[str, str] | None = None,
        file_store: Mapping[str, str] | None = None,
        ephemeral_store: EphemeralSecretStore | Mapping[str, str] | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._env = env if env is not None else {}
        self._secret_store = secret_store if secret_store is not None else {}
        self._file_store = file_store if file_store is not None else {}
        self._ephemeral_store = (
            ephemeral_store
            if isinstance(ephemeral_store, EphemeralSecretStore)
            else EphemeralSecretStore(ephemeral_store)
            if ephemeral_store is not None
            else EphemeralSecretStore()
        )
        self._tool_registry = tool_registry if tool_registry is not None else DEFAULT_TOOL_REGISTRY

    def _validate_sandbox_grant_invariants(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
    ) -> None:
        """Enforce sandbox secret network audience and publication invariants on the grant."""
        network = getattr(grant, "network", None)
        allowed_egress_hosts = tuple(getattr(grant, "allowed_egress_hosts", ()))
        allowed_tools = tuple(getattr(grant, "allowed_tools", ()))

        # 1. Network policy checks
        net_val = (
            network.value if (network is not None and hasattr(network, "value")) else str(network)
        )
        if net_val == "public_https_proxy":
            raise CapabilityViolationError(
                f"PUBLIC_HTTPS_PROXY network is forbidden when resolving sandbox secret "
                f"{definition.name!r}"
            )
        if net_val == "allowlisted_hosts":
            if not allowed_egress_hosts:
                raise CapabilityViolationError(
                    f"ALLOWLISTED_HOSTS network requires non-empty allowed_egress_hosts "
                    f"when resolving {definition.name!r}"
                )
            if not set(allowed_egress_hosts).issubset(set(definition.permitted_egress_hosts)):
                raise CapabilityViolationError(
                    f"Grant allowed_egress_hosts {allowed_egress_hosts} exceeds permitted "
                    f"egress hosts {definition.permitted_egress_hosts} for secret "
                    f"{definition.name!r}"
                )
        elif net_val == "disabled":
            if allowed_egress_hosts:
                raise CapabilityViolationError(
                    "DISABLED network cannot specify allowed_egress_hosts"
                )

        # 2. Publication coupling check
        if self._tool_registry is not None and allowed_tools:
            has_pub_tool = any(
                (tool := self._tool_registry.get_tool(t)) is not None
                and ToolCapabilityTag.AUTOMATED_EXTERNAL_PUBLICATION in tool.capability_tags
                for t in allowed_tools
            )
            if has_pub_tool:
                raise CapabilityViolationError(
                    f"Cannot resolve sandbox secret {definition.name!r} when grant permits "
                    f"automated external publication tools"
                )

    def resolve_for_sandbox(
        self,
        ref: SecretRef,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        """Resolve a secret for container injection, failing closed if unauthorized."""
        definition = self._registry.require(ref.name)

        if definition.exposure_policy == SecretExposurePolicy.BROKER_ONLY:
            raise BrokerOnlySecretExposureError(
                f"Secret {ref.name!r} is BROKER_ONLY and cannot be resolved for sandbox injection"
            )

        self._validate_sandbox_grant_invariants(definition, grant)
        return self._resolve_internal(definition, grant, redactor=redactor)

    def resolve_for_broker(
        self,
        ref_or_name: SecretRef | str,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        """Resolve a secret for broker-side execution only."""
        name = ref_or_name.name if isinstance(ref_or_name, SecretRef) else ref_or_name
        definition = self._registry.require(name)
        return self._resolve_internal(definition, grant, redactor=redactor)

    def _resolve_internal(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        # Dual-key authorization check
        allowed_secret_refs = getattr(grant, "allowed_secret_refs", ())
        granted_secret_scopes = getattr(grant, "granted_secret_scopes", ())
        if definition.name not in allowed_secret_refs:
            raise UnauthorizedSecretError(
                f"Secret {definition.name!r} is not in grant.allowed_secret_refs"
            )
        if definition.required_scope not in granted_secret_scopes:
            scope_val = definition.required_scope.value
            raise MissingSecretScopeError(
                f"Grant lacks required scope {scope_val!r} for secret {definition.name!r}"
            )

        # Lookup secret material from source
        if definition.source == SecretSource.ENV:
            if definition.source_key not in self._env:
                raise SecretNotFoundError(
                    f"Env var {definition.source_key!r} not set for secret {definition.name!r}"
                )
            value = self._env[definition.source_key]
        elif definition.source == SecretSource.SECRET_STORE:
            if definition.source_key not in self._secret_store:
                raise SecretNotFoundError(
                    f"Key {definition.source_key!r} not in secret store for {definition.name!r}"
                )
            value = self._secret_store[definition.source_key]
        elif definition.source == SecretSource.FILE:
            if definition.source_key not in self._file_store:
                raise SecretNotFoundError(
                    f"File key {definition.source_key!r} not in file store for {definition.name!r}"
                )
            value = self._file_store[definition.source_key]
        elif definition.source == SecretSource.EPHEMERAL:
            val = self._ephemeral_store.get(definition.source_key)
            if val is None:
                raise SecretNotFoundError(
                    f"Key {definition.source_key!r} not found in ephemeral secret store "
                    f"for {definition.name!r}"
                )
            value = val
        else:
            raise SecretResolutionError(f"Unsupported secret source: {definition.source!r}")

        if redactor is not None:
            redactor.register(value)

        return ResolvedSecret(
            name=definition.name,
            scope=definition.required_scope,
            value=value,
            destination_env_var=definition.destination_env_var,
            destination_mount_path=definition.destination_mount_path,
        )


__all__ = [
    "ALLOWED_SECRET_ENV_VARS",
    "DEFAULT_SECRET_REGISTRY",
    "BrokerOnlySecretExposureError",
    "CapabilityViolationError",
    "EphemeralSecretHandle",
    "EphemeralSecretStore",
    "InMemoryEphemeralSecretStore",
    "MissingSecretScopeError",
    "RegisteredSecretDefinition",
    "ResolvedSecret",
    "SecretExposurePolicy",
    "SecretNotFoundError",
    "SecretRef",
    "SecretRegistry",
    "SecretResolutionError",
    "SecretResolver",
    "SecretScope",
    "SecretSource",
    "UnauthorizedSecretError",
    "normalize_fqdn",
]

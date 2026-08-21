"""Sandbox secret references, registered definitions, and resolution engine."""

from __future__ import annotations

import abc
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
    pass


class UnauthorizedSecretError(SecretResolutionError):
    pass


class MissingSecretScopeError(SecretResolutionError):
    pass


class MissingTaskContextError(SecretResolutionError):
    """Raised when required task_id context is missing for ephemeral secret access."""


class SecretNotFoundError(SecretResolutionError):
    pass


class BrokerOnlySecretExposureError(SecretResolutionError):
    pass


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


class EphemeralSecretRecord(BaseModel):
    """Complete metadata and secret material stored in the out-of-history ephemeral store."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    handle_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    value: str = Field(min_length=1, repr=False)
    required_scope: SecretScope
    exposure_policy: SecretExposurePolicy = SecretExposurePolicy.SANDBOX_ENV
    permitted_egress_hosts: tuple[str, ...] = ()
    destination_env_var: str | None = None
    destination_mount_path: str | None = None

    def to_registered_definition(self) -> RegisteredSecretDefinition:
        return RegisteredSecretDefinition(
            name=self.handle_id,
            source=SecretSource.EPHEMERAL,
            source_key=self.handle_id,
            required_scope=self.required_scope,
            exposure_policy=self.exposure_policy,
            permitted_egress_hosts=self.permitted_egress_hosts,
            destination_env_var=self.destination_env_var,
            destination_mount_path=self.destination_mount_path,
        )


class EphemeralSecretStore(abc.ABC):
    """Storage interface for ephemeral legacy secrets outside durable workflow history."""

    @abc.abstractmethod
    def store_record(self, record: EphemeralSecretRecord, *, ttl_seconds: int = 3600) -> str:
        pass

    @abc.abstractmethod
    def store(
        self,
        key: str,
        value: str,
        *,
        task_id: str,
        scope: SecretScope = SecretScope.CUSTOM,
        exposure_policy: SecretExposurePolicy = SecretExposurePolicy.SANDBOX_ENV,
        ttl_seconds: int = 3600,
    ) -> str:
        pass

    @abc.abstractmethod
    def get_record(
        self, handle_id: str, *, task_id: str | None = None
    ) -> EphemeralSecretRecord | None:
        pass

    @abc.abstractmethod
    def get(self, handle_or_key: str, *, task_id: str | None = None) -> str | None:
        pass

    @abc.abstractmethod
    def has(self, handle_or_key: str, *, task_id: str | None = None) -> bool:
        pass

    @abc.abstractmethod
    def remove(self, handle_or_key: str, *, task_id: str | None = None) -> None:
        pass

    @abc.abstractmethod
    def refresh_task_ttl(self, task_id: str, *, ttl_seconds: int = 3600) -> None:
        """Refresh the TTL of all ephemeral secrets for the given task."""
        pass

    @abc.abstractmethod
    def delete_task_secrets(self, task_id: str) -> None:
        """Delete all ephemeral secrets for the given task."""
        pass


class InMemoryEphemeralSecretStore(EphemeralSecretStore):
    """In-memory ephemeral secret store implementation for testing and local runtime."""

    def __init__(
        self, initial_records: Mapping[str, EphemeralSecretRecord | str] | None = None
    ) -> None:
        self._records: dict[str, EphemeralSecretRecord] = {}
        for k, v in (initial_records or {}).items():
            self._records[k] = (
                v
                if isinstance(v, EphemeralSecretRecord)
                else EphemeralSecretRecord(
                    handle_id=k,
                    task_id="default_task",
                    value=v,
                    required_scope=SecretScope.CUSTOM,
                    exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
                )
            )

    def store_record(self, record: EphemeralSecretRecord, *, ttl_seconds: int = 3600) -> str:
        self._records[record.handle_id] = record
        return record.handle_id

    def store(
        self,
        key: str,
        value: str,
        *,
        task_id: str,
        scope: SecretScope = SecretScope.CUSTOM,
        exposure_policy: SecretExposurePolicy = SecretExposurePolicy.SANDBOX_ENV,
        ttl_seconds: int = 3600,
    ) -> str:
        if not task_id or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        return self.store_record(
            EphemeralSecretRecord(
                handle_id=key,
                task_id=task_id,
                value=value,
                required_scope=scope,
                exposure_policy=exposure_policy,
            ),
            ttl_seconds=ttl_seconds,
        )

    def get_record(
        self, handle_id: str, *, task_id: str | None = None
    ) -> EphemeralSecretRecord | None:
        if not task_id:
            return None
        rec = self._records.get(handle_id)
        if rec is None or rec.task_id != task_id:
            return None
        return rec

    def get(self, handle_or_key: str, *, task_id: str | None = None) -> str | None:
        rec = self.get_record(handle_or_key, task_id=task_id)
        return rec.value if rec is not None else None

    def has(self, handle_or_key: str, *, task_id: str | None = None) -> bool:
        return self.get_record(handle_or_key, task_id=task_id) is not None

    def remove(self, handle_or_key: str, *, task_id: str | None = None) -> None:
        if not task_id:
            return
        rec = self._records.get(handle_or_key)
        if rec is not None and rec.task_id == task_id:
            self._records.pop(handle_or_key, None)

    def refresh_task_ttl(self, task_id: str, *, ttl_seconds: int = 3600) -> None:
        pass

    def delete_task_secrets(self, task_id: str) -> None:
        keys_to_delete = [k for k, rec in self._records.items() if rec.task_id == task_id]
        for k in keys_to_delete:
            self._records.pop(k, None)

    def clear(self) -> None:
        self._records.clear()

    def __contains__(self, handle_or_key: str) -> bool:
        return handle_or_key in self._records

    def __len__(self) -> int:
        return len(self._records)


def normalize_fqdn(host: str) -> str:
    """Normalize and validate a fully-qualified domain name (FQDN)."""
    try:
        ascii_host = host.strip().rstrip(".").lower().encode("idna").decode("ascii")
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
            if not _RE_SAFE_NAME.match(key) or key in seen_keys:
                raise ValueError(f"Invalid or duplicate metadata key: {key!r}")
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
        if not _RE_SAFE_ENV_VAR.match(v) or not (
            v in ALLOWED_SECRET_ENV_VARS or v.startswith(_SECRET_ENV_VAR_PREFIX)
        ):
            raise ValueError(
                f"destination_env_var must be in {sorted(ALLOWED_SECRET_ENV_VARS)} "
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
        return (
            f"/run/secrets/code-agent/{self.destination_mount_name}"
            if self.destination_mount_name
            else None
        )

    @model_validator(mode="after")
    def _validate_destination_consistency(self) -> RegisteredSecretDefinition:
        if self.source == SecretSource.FILE:
            if not self.source_key or self.source_key.startswith("/"):
                raise ValueError("FILE secret source_key must be a relative path")
            parts = self.source_key.split("/")
            if ".." in parts or "." in parts or "" in parts:
                raise ValueError(
                    f"FILE secret source_key cannot contain traversal: {self.source_key!r}"
                )
        if self.exposure_policy == SecretExposurePolicy.BROKER_ONLY:
            if self.destination_env_var or self.destination_mount_name:
                raise ValueError(
                    "BROKER_ONLY secrets cannot specify sandbox injection destinations"
                )
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_ENV:
            if self.destination_env_var is None or self.destination_mount_name is not None:
                raise ValueError("SANDBOX_ENV secrets require destination_env_var only")
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_FILE:
            if self.destination_mount_name is None or self.destination_env_var is not None:
                raise ValueError("SANDBOX_FILE secrets require destination_mount_path only")
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

    __str__ = __repr__


class SecretRegistry:
    """Authoritative server-side registry of registered secret definitions."""

    def __init__(
        self,
        definitions: Sequence[RegisteredSecretDefinition] = (),
        *,
        ephemeral_store: EphemeralSecretStore | None = None,
        task_id: str | None = None,
    ) -> None:
        self._definitions: dict[str, RegisteredSecretDefinition] = {}
        self._ephemeral_store = ephemeral_store
        self._task_id = task_id
        for d in definitions:
            self.register(d)

    def register(self, definition: RegisteredSecretDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"Secret {definition.name!r} already registered")
        self._definitions[definition.name] = definition

    def get(self, name: str, *, task_id: str | None = None) -> RegisteredSecretDefinition | None:
        if name in self._definitions:
            return self._definitions[name]
        eff_task = task_id or self._task_id
        if self._ephemeral_store and name.startswith("ephem_") and eff_task:
            if record := self._ephemeral_store.get_record(name, task_id=eff_task):
                return record.to_registered_definition()
        return None

    def require(self, name: str, *, task_id: str | None = None) -> RegisteredSecretDefinition:
        definition = self.get(name, task_id=task_id)
        if definition is None:
            raise SecretNotFoundError(f"Secret {name!r} not found in registry")
        return definition

    def __len__(self) -> int:
        return len(self._definitions)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __iter__(self) -> Iterator[RegisteredSecretDefinition]:
        return iter(self._definitions.values())


DEFAULT_SECRET_REGISTRY: Final[SecretRegistry] = SecretRegistry()


class SecretResolver:
    """Resolves secret definitions into ResolvedSecret instances under a capability grant."""

    def __init__(
        self,
        registry: SecretRegistry,
        *,
        task_id: str | None = None,
        env: Mapping[str, str] | None = None,
        secret_store: Mapping[str, str] | None = None,
        file_store: Mapping[str, str] | None = None,
        ephemeral_store: EphemeralSecretStore | Mapping[str, str] | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._task_id = task_id
        self._env = env or {}
        self._secret_store = secret_store or {}
        self._file_store = file_store or {}
        self._ephemeral_store = (
            ephemeral_store
            if isinstance(ephemeral_store, EphemeralSecretStore)
            else InMemoryEphemeralSecretStore(ephemeral_store)
        )
        self._tool_registry = tool_registry or DEFAULT_TOOL_REGISTRY
        if self._registry._ephemeral_store is None:
            self._registry._ephemeral_store = self._ephemeral_store
        if self._registry._task_id is None:
            self._registry._task_id = self._task_id

    def _validate_sandbox_grant_invariants(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
    ) -> None:
        """Enforce sandbox secret network audience and publication invariants on the grant."""
        network = getattr(grant, "network", None)
        allowed_egress_hosts = tuple(getattr(grant, "allowed_egress_hosts", ()))
        allowed_tools = tuple(getattr(grant, "allowed_tools", ()))

        net_val = (
            network.value if (network is not None and hasattr(network, "value")) else str(network)
        )
        if net_val == "public_https_proxy":
            raise CapabilityViolationError(
                f"PUBLIC_HTTPS_PROXY network is forbidden when resolving {definition.name!r}"
            )
        if net_val == "allowlisted_hosts":
            if not allowed_egress_hosts:
                raise CapabilityViolationError("ALLOWLISTED_HOSTS requires allowed_egress_hosts")
            if not set(allowed_egress_hosts).issubset(set(definition.permitted_egress_hosts)):
                raise CapabilityViolationError(
                    f"Egress hosts {allowed_egress_hosts} exceed permitted for {definition.name!r}"
                )
        elif net_val == "disabled" and allowed_egress_hosts:
            raise CapabilityViolationError("DISABLED network cannot specify allowed_egress_hosts")

        if self._tool_registry is not None and allowed_tools:
            if any(
                (t_obj := self._tool_registry.get_tool(t)) is not None
                and ToolCapabilityTag.AUTOMATED_EXTERNAL_PUBLICATION in t_obj.capability_tags
                for t in allowed_tools
            ):
                raise CapabilityViolationError(
                    f"Cannot resolve sandbox secret {definition.name!r} with publication tools"
                )

    def resolve_for_sandbox(
        self,
        ref_or_name: SecretRef | str,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        """Resolve a secret for container injection, failing closed if unauthorized."""
        name = ref_or_name.name if isinstance(ref_or_name, SecretRef) else ref_or_name
        definition = self._registry.require(name, task_id=self._task_id)

        if definition.exposure_policy == SecretExposurePolicy.BROKER_ONLY:
            raise BrokerOnlySecretExposureError(
                f"Secret {name!r} is BROKER_ONLY and cannot be resolved for sandbox injection"
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
        definition = self._registry.require(name, task_id=self._task_id)
        return self._resolve_internal(definition, grant, redactor=redactor)

    def _resolve_internal(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        allowed_secret_refs = getattr(grant, "allowed_secret_refs", ())
        granted_secret_scopes = getattr(grant, "granted_secret_scopes", ())
        if definition.name not in allowed_secret_refs:
            raise UnauthorizedSecretError(
                f"Secret {definition.name!r} is not in grant.allowed_secret_refs"
            )
        if definition.required_scope not in granted_secret_scopes:
            scope_val = definition.required_scope.value
            raise MissingSecretScopeError(
                f"Grant lacks required scope {scope_val!r} for {definition.name!r}"
            )

        if definition.source == SecretSource.ENV:
            value = self._env.get(definition.source_key)
        elif definition.source == SecretSource.SECRET_STORE:
            value = self._secret_store.get(definition.source_key)
        elif definition.source == SecretSource.FILE:
            value = self._file_store.get(definition.source_key)
        elif definition.source == SecretSource.EPHEMERAL:
            if not self._task_id:
                raise MissingTaskContextError(f"Missing task_id context for {definition.name!r}")
            value = self._ephemeral_store.get(definition.source_key, task_id=self._task_id)
        else:
            raise SecretResolutionError(f"Unsupported secret source: {definition.source!r}")

        if value is None:
            raise SecretNotFoundError(
                f"Secret material for {definition.name!r} not found in source"
            )

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
    "EphemeralSecretRecord",
    "EphemeralSecretStore",
    "InMemoryEphemeralSecretStore",
    "MissingSecretScopeError",
    "MissingTaskContextError",
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

"""Sandbox secret references, registered definitions, and resolution engine."""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sandbox.redact import SecretRedactor

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


class SecretResolutionError(RuntimeError):
    """Base exception for secret resolution failures."""


class UnauthorizedSecretError(SecretResolutionError):
    """Raised when access to a secret is not authorized by the capability grant."""


class MissingSecretScopeError(SecretResolutionError):
    """Raised when a capability grant lacks the scope required by a secret."""


class SecretNotFoundError(SecretResolutionError):
    """Raised when a requested secret is not found in the registry or source."""


class BrokerOnlySecretExposureError(SecretResolutionError):
    """Raised when attempting to resolve a broker-only secret for sandbox injection."""


class ConflictingSecretDeclarationError(SecretResolutionError):
    """Raised when legacy secrets and modern secret_refs declare conflicting keys."""


class DeprecatedLegacySecretsError(SecretResolutionError):
    """Raised when legacy raw secrets are used after the deprecation cutoff."""


class SecretSource(enum.StrEnum):
    """Supported backing sources for registered secrets."""

    ENV = "env"
    SECRET_STORE = "secret_store"
    FILE = "file"


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

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    source: SecretSource
    source_key: str = Field(min_length=1)
    required_scope: SecretScope
    exposure_policy: SecretExposurePolicy = SecretExposurePolicy.BROKER_ONLY
    permitted_egress_hosts: tuple[str, ...] = ()
    destination_env_var: str | None = None
    destination_mount_path: str | None = None

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

    @field_validator("destination_mount_path")
    @classmethod
    def _validate_mount_path(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _RE_SAFE_FILENAME.match(v) or ".." in v or v.startswith(("/", ".")):
            raise ValueError(f"Invalid destination mount path safe name: {v!r}")
        return f"/run/secrets/code-agent/{v}"

    @model_validator(mode="after")
    def _validate_destination_consistency(self) -> RegisteredSecretDefinition:
        if self.exposure_policy == SecretExposurePolicy.BROKER_ONLY:
            if self.destination_env_var is not None or self.destination_mount_path is not None:
                raise ValueError(
                    "BROKER_ONLY secrets cannot specify sandbox injection destinations"
                )
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_ENV:
            if self.destination_env_var is None:
                raise ValueError("SANDBOX_ENV secrets must specify destination_env_var")
            if self.destination_mount_path is not None:
                raise ValueError("SANDBOX_ENV secrets cannot specify destination_mount_path")
        elif self.exposure_policy == SecretExposurePolicy.SANDBOX_FILE:
            if self.destination_mount_path is None:
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
        self._value = value
        self._destination_env_var = destination_env_var
        self._destination_mount_path = destination_mount_path

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


class SecretResolver:
    """Resolves secret definitions into ResolvedSecret instances under a capability grant."""

    def __init__(
        self,
        registry: SecretRegistry,
        *,
        env: Mapping[str, str] | None = None,
        secret_store: Mapping[str, str] | None = None,
        file_store: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._env = env if env is not None else {}
        self._secret_store = secret_store if secret_store is not None else {}
        self._file_store = file_store if file_store is not None else {}

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

        return self._resolve_internal(definition, grant, redactor=redactor)

    def resolve_for_broker(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        """Resolve a secret for broker-side execution only."""
        return self._resolve_internal(definition, grant, redactor=redactor)

    def _resolve_internal(
        self,
        definition: RegisteredSecretDefinition,
        grant: Any,
        *,
        redactor: SecretRedactor | None = None,
    ) -> ResolvedSecret:
        # Dual-key authorization check
        if definition.name not in grant.allowed_secret_refs:
            raise UnauthorizedSecretError(
                f"Secret {definition.name!r} is not in grant.allowed_secret_refs"
            )
        if definition.required_scope not in grant.granted_secret_scopes:
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


class LegacyIngressTaskRequest(BaseModel):
    """Ingress-only task request DTO for legacy raw-secret intake."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_text: str = Field(min_length=1)
    secret_refs: tuple[SecretRef, ...] = ()
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)

    @model_validator(mode="before")
    @classmethod
    def _scrub_raw_secret_values(cls, data: Any) -> Any:
        if isinstance(data, dict) and "secrets" in data and isinstance(data["secrets"], dict):
            scrubbed_secrets = {}
            for k in data["secrets"]:
                scrubbed_secrets[k] = "[REDACTED_AT_INGRESS]"
            data = dict(data)
            data["secrets"] = scrubbed_secrets
        return data

    @field_validator("secrets")
    @classmethod
    def _sanitize_legacy_secrets(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _RE_SAFE_NAME.match(key):
                raise ValueError("Invalid secret key format in legacy secrets")
        return v


class IngressMigrationAdapter:
    """Adapts legacy ingress requests by stripping raw values and producing clean SecretRefs."""

    @classmethod
    def adapt(
        cls,
        request: LegacyIngressTaskRequest,
        *,
        reject_legacy_secrets: bool = False,
    ) -> tuple[SecretRef, ...]:
        """Extract logical secret names, discarding raw values immediately."""
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

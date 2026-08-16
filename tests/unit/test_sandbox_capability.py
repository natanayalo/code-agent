"""Unit tests for sandbox capability grants, secret references, and resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox.capability import (
    BrokerOnlySecretExposureError,
    CapabilityGrantFactory,
    CapabilityViolationError,
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    FileSystemAccessPolicy,
    IngressMigrationAdapter,
    LegacyIngressTaskRequest,
    MissingSecretScopeError,
    NetworkEgressPolicy,
    RegisteredSecretDefinition,
    ResolvedSecret,
    ResourceLimits,
    SandboxCapabilityGrant,
    SecretExposurePolicy,
    SecretNotFoundError,
    SecretRef,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
    UnauthorizedSecretError,
    normalize_fqdn,
    parse_memory_bytes,
)
from sandbox.redact import SecretRedactor


def test_resource_limits_parsing_and_bounds() -> None:
    # Safe defaults
    limits = ResourceLimits()
    assert limits.cpu_limit == 1.0
    assert limits.memory_bytes == 1073741824
    assert limits.pids_limit == 256
    assert limits.timeout_seconds == 600

    # String memory parsing
    parsed_1g = ResourceLimits(memory_bytes="1g")  # type: ignore[arg-type]
    assert parsed_1g.memory_bytes == 1073741824
    parsed_512m = ResourceLimits(memory_bytes="512MiB")  # type: ignore[arg-type]
    assert parsed_512m.memory_bytes == 512 * 1024 * 1024

    # Invalid / out of bounds memory
    with pytest.raises(ValidationError):
        ResourceLimits(memory_bytes="10m")  # below 64MiB
    with pytest.raises(ValidationError):
        ResourceLimits(memory_bytes="16g")  # above 8GiB
    with pytest.raises(ValidationError):
        ResourceLimits(memory_bytes="invalid")  # type: ignore[arg-type]

    # CPU validation
    with pytest.raises(ValidationError):
        ResourceLimits(cpu_limit=0.05)
    with pytest.raises(ValidationError):
        ResourceLimits(cpu_limit=5.0)
    with pytest.raises(ValidationError):
        ResourceLimits(cpu_limit=float("nan"))
    with pytest.raises(ValidationError):
        ResourceLimits(cpu_limit=float("inf"))

    # Extra fields forbidden & immutability
    with pytest.raises(ValidationError):
        ResourceLimits(unknown_field="val")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        limits.cpu_limit = 2.0  # type: ignore[misc]


def test_parse_memory_bytes_edge_cases() -> None:
    assert parse_memory_bytes(100 * 1024 * 1024) == 100 * 1024 * 1024
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_memory_bytes("")
    with pytest.raises(ValueError, match="Unsupported memory limit type"):
        parse_memory_bytes(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive and finite"):
        parse_memory_bytes("-500mb")


def test_normalize_fqdn() -> None:
    assert normalize_fqdn("api.github.com.") == "api.github.com"
    assert normalize_fqdn("API.GITHUB.COM") == "api.github.com"
    assert normalize_fqdn("sub.domain.example.com") == "sub.domain.example.com"
    with pytest.raises(ValueError, match="Invalid FQDN format"):
        normalize_fqdn("localhost")
    with pytest.raises(ValueError, match="Invalid FQDN format"):
        normalize_fqdn("192.168.1.1")


def test_secret_ref_validation_and_immutability() -> None:
    ref = SecretRef(name="github_token", metadata=(("purpose", "pr_delivery"),))
    assert ref.name == "github_token"
    assert ref.metadata == (("purpose", "pr_delivery"),)

    # Invalid name
    with pytest.raises(ValidationError):
        SecretRef(name="bad/name")

    # Extra fields forbidden
    with pytest.raises(ValidationError):
        SecretRef(name="github_token", scope="provider_auth")  # type: ignore[call-arg]

    # Metadata bounds and duplicate keys
    with pytest.raises(ValidationError):
        SecretRef(name="github_token", metadata=(("k", "v1"), ("k", "v2")))  # duplicate key
    with pytest.raises(ValidationError):
        SecretRef(
            name="github_token", metadata=tuple((f"k{i}", "v") for i in range(10))
        )  # > 8 entries
    with pytest.raises(ValidationError):
        SecretRef(name="github_token", metadata=(("k", "v\nwith_control_char"),))  # control char


def test_registered_secret_definition_consistency() -> None:
    # Broker only
    broker_sec = RegisteredSecretDefinition(
        name="github_token",
        source=SecretSource.ENV,
        source_key="GH_TOKEN",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
        permitted_egress_hosts=("api.github.com",),
    )
    assert broker_sec.exposure_policy == SecretExposurePolicy.BROKER_ONLY

    # Broker only cannot have destinations
    with pytest.raises(ValidationError, match="BROKER_ONLY"):
        RegisteredSecretDefinition(
            name="github_token",
            source=SecretSource.ENV,
            source_key="GH_TOKEN",
            required_scope=SecretScope.GIT_PUSH,
            exposure_policy=SecretExposurePolicy.BROKER_ONLY,
            destination_env_var="GH_TOKEN",
        )

    # Sandbox env
    env_sec = RegisteredSecretDefinition(
        name="openai_key",
        source=SecretSource.ENV,
        source_key="OPENAI_API_KEY",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    assert env_sec.destination_env_var == "OPENAI_API_KEY"

    # Sandbox env requires env var and disallows mount path
    with pytest.raises(ValidationError, match="SANDBOX_ENV"):
        RegisteredSecretDefinition(
            name="openai_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        )
    with pytest.raises(ValidationError, match="destination_env_var"):
        RegisteredSecretDefinition(
            name="openai_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="UNAPPROVED_CUSTOM_VAR",
        )

    # Sandbox file
    file_sec = RegisteredSecretDefinition(
        name="gemini_oauth",
        source=SecretSource.FILE,
        source_key="oauth_token.json",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
        destination_mount_path="oauth_token.json",
    )
    assert file_sec.destination_mount_path == "/run/secrets/code-agent/oauth_token.json"


def test_resolved_secret_opacity_and_slots() -> None:
    secret_value = "super-secret-token-12345"
    resolved = ResolvedSecret(
        name="github_token",
        scope=SecretScope.GIT_PUSH,
        value=secret_value,
        destination_env_var="GH_TOKEN",
    )

    # Slots verify no __dict__
    assert not hasattr(resolved, "__dict__")
    with pytest.raises(TypeError):
        vars(resolved)

    # __repr__ and __str__ redaction
    assert "super-secret-token" not in repr(resolved)
    assert "super-secret-token" not in str(resolved)
    assert "redacted=True" in repr(resolved)

    # Access via reveal_secret_value
    assert resolved.reveal_secret_value() == secret_value

    # Traceback test ensuring value is not in exception representations
    try:
        raise ValueError(f"Failed handling {resolved}")
    except ValueError as err:
        assert secret_value not in str(err)


def test_sandbox_capability_grant_defaults_and_cross_field() -> None:
    grant = SandboxCapabilityGrant()
    assert grant.network == NetworkEgressPolicy.DISABLED
    assert grant.filesystem == FileSystemAccessPolicy.SCRATCH_ONLY
    assert grant.allow_dangerous_shell is False
    assert grant.allowed_tools == ()
    assert grant.allowed_secret_refs == ()
    assert grant.granted_secret_scopes == frozenset()
    assert grant.allowed_egress_hosts == ()

    # Immutability
    with pytest.raises(ValidationError):
        grant.allow_dangerous_shell = True  # type: ignore[misc]

    # Cross-field validation: ALLOWLISTED_HOSTS requires hosts
    with pytest.raises(ValidationError, match="ALLOWLISTED_HOSTS"):
        SandboxCapabilityGrant(network=NetworkEgressPolicy.ALLOWLISTED_HOSTS)

    # DISABLED network cannot have hosts
    with pytest.raises(ValidationError, match="DISABLED"):
        SandboxCapabilityGrant(
            network=NetworkEgressPolicy.DISABLED, allowed_egress_hosts=("api.github.com",)
        )


def test_capability_grant_factory_network_and_secret_audience() -> None:
    sec_github = RegisteredSecretDefinition(
        name="github_token",
        source=SecretSource.ENV,
        source_key="GH_TOKEN",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts=("api.github.com", "github.com"),
        destination_env_var="GH_TOKEN",
    )
    sec_internal = RegisteredSecretDefinition(
        name="internal_token",
        source=SecretSource.ENV,
        source_key="INT_TOKEN",
        required_scope=SecretScope.API_INGRESS,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts=("api.github.com", "internal.service.com"),
        destination_env_var="CODE_AGENT_SECRET_INT_TOKEN",
    )

    # 1. DISABLED network with sandbox secrets is valid
    grant_disabled = CapabilityGrantFactory.create_grant(
        network=NetworkEgressPolicy.DISABLED,
        allowed_secret_defs=(sec_github,),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    assert grant_disabled.network == NetworkEgressPolicy.DISABLED
    assert grant_disabled.allowed_egress_hosts == ()

    # 2. PUBLIC_HTTPS_PROXY with sandbox secrets is rejected
    with pytest.raises(CapabilityViolationError, match="PUBLIC_HTTPS_PROXY is forbidden"):
        CapabilityGrantFactory.create_grant(
            network=NetworkEgressPolicy.PUBLIC_HTTPS_PROXY,
            allowed_secret_defs=(sec_github,),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
        )

    # 3. ALLOWLISTED_HOSTS with single secret
    grant_allowed = CapabilityGrantFactory.create_grant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        allowed_secret_defs=(sec_github,),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
        allowed_egress_hosts=("api.github.com",),
    )
    assert grant_allowed.allowed_egress_hosts == ("api.github.com",)

    # Exceeding audience rejected
    with pytest.raises(CapabilityViolationError, match="exceeds sandbox secret audience"):
        CapabilityGrantFactory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_defs=(sec_github,),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=("api.github.com", "evil.attacker.com"),
        )

    # 4. Multi-secret audience intersection:
    # Intersection of ("api.github.com", "github.com") and
    # ("api.github.com", "internal.service.com") is ("api.github.com",)
    grant_multi = CapabilityGrantFactory.create_grant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        allowed_secret_defs=(sec_github, sec_internal),
        granted_secret_scopes=(SecretScope.GIT_PUSH, SecretScope.API_INGRESS),
        allowed_egress_hosts=("api.github.com",),
    )
    assert grant_multi.allowed_egress_hosts == ("api.github.com",)

    with pytest.raises(CapabilityViolationError, match="exceeds sandbox secret audience"):
        CapabilityGrantFactory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_defs=(sec_github, sec_internal),
            granted_secret_scopes=(SecretScope.GIT_PUSH, SecretScope.API_INGRESS),
            allowed_egress_hosts=("github.com",),  # not in internal_token's audience
        )


def test_capability_grant_factory_dangerous_shell_and_publication_coupling() -> None:
    # Dangerous shell bidirectional check
    grant_shell = CapabilityGrantFactory.create_grant(
        allowed_tools=("execute_shell", "file_editor")
    )
    assert grant_shell.allow_dangerous_shell is True

    grant_safe = CapabilityGrantFactory.create_grant(allowed_tools=("file_editor",))
    assert grant_safe.allow_dangerous_shell is False

    # Publication coupling check: sandbox secrets cannot combine with automated publication tools
    sandbox_sec = RegisteredSecretDefinition(
        name="env_secret",
        source=SecretSource.ENV,
        source_key="OPENAI_API_KEY",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    # Git publication tool rejected
    with pytest.raises(CapabilityViolationError, match="automated external publication"):
        CapabilityGrantFactory.create_grant(
            allowed_tools=("execute_git", "file_editor"),
            allowed_secret_defs=(sandbox_sec,),
            granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
        )
    # Non-Git publication tool rejected
    with pytest.raises(CapabilityViolationError, match="automated external publication"):
        CapabilityGrantFactory.create_grant(
            allowed_tools=("upload_artifact", "file_editor"),
            allowed_secret_defs=(sandbox_sec,),
            granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
        )


def test_secret_resolver_dual_key_authorization_and_exposure() -> None:
    broker_sec = RegisteredSecretDefinition(
        name="github_token",
        source=SecretSource.ENV,
        source_key="GH_TOKEN",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
    )
    sandbox_sec = RegisteredSecretDefinition(
        name="openai_key",
        source=SecretSource.SECRET_STORE,
        source_key="openai_key_store",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    registry = SecretRegistry([broker_sec, sandbox_sec])

    env = {"GH_TOKEN": "gh-secret-token"}
    store = {"openai_key_store": "sk-openai-token"}
    resolver = SecretResolver(registry, env=env, secret_store=store)

    redactor = SecretRedactor()

    # Valid sandbox resolution
    grant_sandbox = CapabilityGrantFactory.create_grant(
        allowed_secret_defs=(sandbox_sec,),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    resolved_sandbox = resolver.resolve_for_sandbox(
        SecretRef(name="openai_key"), grant_sandbox, redactor=redactor
    )
    assert resolved_sandbox.name == "openai_key"
    assert resolved_sandbox.reveal_secret_value() == "sk-openai-token"
    # Redactor registered secret and masks it
    redacted_output = redactor.redact("Result sk-openai-token logged")
    assert "sk-openai-token" not in redacted_output
    assert "[REDACTED]" in redacted_output

    # BROKER_ONLY secret rejected from sandbox resolution
    grant_broker = CapabilityGrantFactory.create_grant(
        allowed_secret_defs=(broker_sec,),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    with pytest.raises(BrokerOnlySecretExposureError, match="BROKER_ONLY"):
        resolver.resolve_for_sandbox(SecretRef(name="github_token"), grant_broker)

    # Valid broker resolution
    resolved_broker = resolver.resolve_for_broker(broker_sec, grant_broker)
    assert resolved_broker.reveal_secret_value() == "gh-secret-token"

    # Dual-key authorization failures
    # 1. Missing name in allowed_secret_refs
    grant_no_name = CapabilityGrantFactory.create_grant(
        allowed_secret_defs=(),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    with pytest.raises(UnauthorizedSecretError, match="not in grant.allowed_secret_refs"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_no_name)

    # 2. Missing scope in granted_secret_scopes
    grant_no_scope = CapabilityGrantFactory.create_grant(
        allowed_secret_defs=(sandbox_sec,),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),  # wrong scope
    )
    with pytest.raises(MissingSecretScopeError, match="Grant lacks required scope"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_no_scope)

    # 3. Missing secret in store
    empty_resolver = SecretResolver(registry, env={}, secret_store={})
    with pytest.raises(SecretNotFoundError):
        empty_resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_sandbox)


def test_ingress_migration_adapter_and_error_sanitization() -> None:
    secret_value = "super-secret-token-xyz"
    legacy_req = LegacyIngressTaskRequest(
        task_text="Run task",
        secrets={"github_token": secret_value},
    )

    # Repr of LegacyIngressTaskRequest never exposes secrets
    assert secret_value not in repr(legacy_req)

    # Adapter strips values and produces SecretRef tuple
    refs = IngressMigrationAdapter.adapt(legacy_req)
    assert len(refs) == 1
    assert refs[0].name == "github_token"

    # Conflicting declarations
    conflict_req = LegacyIngressTaskRequest(
        task_text="Run task",
        secret_refs=(SecretRef(name="github_token"),),
        secrets={"github_token": secret_value},
    )
    with pytest.raises(ConflictingSecretDeclarationError, match="Conflicting secret declarations"):
        IngressMigrationAdapter.adapt(conflict_req)

    # Deprecation rejection mode (M29 setting)
    with pytest.raises(DeprecatedLegacySecretsError, match="no longer accepted"):
        IngressMigrationAdapter.adapt(legacy_req, reject_legacy_secrets=True)

    # Malformed legacy input error sanitization test
    with pytest.raises(ValidationError) as exc_info:
        LegacyIngressTaskRequest(
            task_text="Run task",
            secrets={"invalid/key/name": secret_value},
        )
    # The error message should not leak the secret value
    assert secret_value not in str(exc_info.value)


def test_secret_resolver_file_source_and_registry_edge_cases() -> None:
    file_sec = RegisteredSecretDefinition(
        name="ca_cert",
        source=SecretSource.FILE,
        source_key="custom_ca.pem",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
        destination_mount_path="custom_ca.pem",
    )
    registry = SecretRegistry([file_sec])

    # Registry duplicate
    with pytest.raises(ValueError, match="already registered"):
        registry.register(file_sec)

    # Registry get None and require missing
    assert registry.get("missing_key") is None
    with pytest.raises(SecretNotFoundError, match="not found in registry"):
        registry.require("missing_key")

    # File store resolution
    resolver = SecretResolver(registry, file_store={"custom_ca.pem": "CERT-CONTENT-XYZ"})
    grant = CapabilityGrantFactory.create_grant(
        allowed_secret_defs=(file_sec,),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    resolved = resolver.resolve_for_sandbox(SecretRef(name="ca_cert"), grant)
    assert resolved.name == "ca_cert"
    assert resolved.scope == SecretScope.CUSTOM
    assert resolved.destination_mount_path == "/run/secrets/code-agent/custom_ca.pem"
    assert resolved.destination_env_var is None
    assert resolved.reveal_secret_value() == "CERT-CONTENT-XYZ"

    # File not found
    empty_file_resolver = SecretResolver(registry, file_store={})
    with pytest.raises(SecretNotFoundError, match="not in file store"):
        empty_file_resolver.resolve_for_sandbox(SecretRef(name="ca_cert"), grant)


def test_resource_limits_and_factory_edge_cases() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(timeout_seconds=0)
    with pytest.raises(ValidationError):
        ResourceLimits(timeout_seconds=5000)
    with pytest.raises(ValidationError):
        ResourceLimits(pids_limit=10)
    with pytest.raises(ValidationError):
        ResourceLimits(pids_limit=2000)

    # CapabilityGrantFactory error paths
    sec = RegisteredSecretDefinition(
        name="github_token",
        source=SecretSource.ENV,
        source_key="GH_TOKEN",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts=("api.github.com",),
        destination_env_var="GH_TOKEN",
    )
    with pytest.raises(CapabilityViolationError, match="requires non-empty allowed_egress_hosts"):
        CapabilityGrantFactory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_defs=(sec,),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=(),
        )

    with pytest.raises(CapabilityViolationError, match="DISABLED network cannot specify"):
        CapabilityGrantFactory.create_grant(
            network=NetworkEgressPolicy.DISABLED,
            allowed_secret_defs=(sec,),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=("api.github.com",),
        )

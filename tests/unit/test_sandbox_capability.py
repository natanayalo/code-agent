"""Unit tests for sandbox capability grants, secret references, and resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox.capability import (
    MANDATORY_DENIED_PATHS,
    SCRATCH_PATH_PREFIX,
    BrokerOnlySecretExposureError,
    CapabilityGrantFactory,
    CapabilityViolationError,
    FileSystemAccessPolicy,
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
    validate_grant_for_execution,
)
from sandbox.redact import SecretRedactor


def test_resource_limits_parsing_and_bounds() -> None:
    # Safe defaults
    limits = ResourceLimits()
    assert limits.cpu_limit == 1.0
    assert limits.memory_bytes == 1073741824
    assert limits.pids_limit == 256
    assert limits.timeout_seconds == 600

    # String memory parsing (integral and fractional)
    parsed_1g = ResourceLimits(memory_bytes="1g")  # type: ignore[arg-type]
    assert parsed_1g.memory_bytes == 1073741824
    parsed_1_5g = ResourceLimits(memory_bytes="1.5g")  # type: ignore[arg-type]
    assert parsed_1_5g.memory_bytes == int(1.5 * 1024 * 1024 * 1024)
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
    assert parse_memory_bytes("1.5gb") == int(1.5 * 1000 * 1000 * 1000)
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
        SecretRef(name="github_token", metadata=(("k", "v1"), ("k", "v2")))
    with pytest.raises(ValidationError):
        SecretRef(name="github_token", metadata=tuple((f"k{i}", "v") for i in range(10)))
    with pytest.raises(ValidationError):
        SecretRef(name="github_token", metadata=(("k", "v\nwith_control_char"),))


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
    assert grant.denied_paths == MANDATORY_DENIED_PATHS

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

    # Mandatory denied paths cannot be removed
    with pytest.raises(ValidationError, match="denied_paths must include mandatory"):
        SandboxCapabilityGrant(denied_paths=("/custom",))


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
    registry = SecretRegistry([sec_github, sec_internal])
    factory = CapabilityGrantFactory(secret_registry=registry)

    # 1. DISABLED network with sandbox secrets is valid
    grant_disabled = factory.create_grant(
        network=NetworkEgressPolicy.DISABLED,
        allowed_secret_refs=(SecretRef(name="github_token"),),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    assert grant_disabled.network == NetworkEgressPolicy.DISABLED
    assert grant_disabled.allowed_egress_hosts == ()

    # 2. PUBLIC_HTTPS_PROXY with sandbox secrets is rejected
    with pytest.raises(CapabilityViolationError, match="PUBLIC_HTTPS_PROXY is forbidden"):
        factory.create_grant(
            network=NetworkEgressPolicy.PUBLIC_HTTPS_PROXY,
            allowed_secret_refs=("github_token",),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
        )

    # 3. ALLOWLISTED_HOSTS with single secret
    grant_allowed = factory.create_grant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        allowed_secret_refs=("github_token",),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
        allowed_egress_hosts=("api.github.com",),
    )
    assert grant_allowed.allowed_egress_hosts == ("api.github.com",)

    # Exceeding audience rejected
    with pytest.raises(CapabilityViolationError, match="exceeds sandbox secret audience"):
        factory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_refs=("github_token",),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=("api.github.com", "evil.attacker.com"),
        )

    # 4. Multi-secret audience intersection:
    grant_multi = factory.create_grant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        allowed_secret_refs=("github_token", "internal_token"),
        granted_secret_scopes=(SecretScope.GIT_PUSH, SecretScope.API_INGRESS),
        allowed_egress_hosts=("api.github.com",),
    )
    assert grant_multi.allowed_egress_hosts == ("api.github.com",)

    with pytest.raises(CapabilityViolationError, match="exceeds sandbox secret audience"):
        factory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_refs=("github_token", "internal_token"),
            granted_secret_scopes=(SecretScope.GIT_PUSH, SecretScope.API_INGRESS),
            allowed_egress_hosts=("github.com",),  # not in internal_token's audience
        )


def test_capability_grant_factory_registry_provenance_and_scope_enforcement() -> None:
    sec_github = RegisteredSecretDefinition(
        name="github_token",
        source=SecretSource.ENV,
        source_key="GH_TOKEN",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts=("api.github.com", "github.com"),
        destination_env_var="GH_TOKEN",
    )
    registry = SecretRegistry([sec_github])
    factory = CapabilityGrantFactory(secret_registry=registry)

    # 1. Authoritative registry provenance: Unregistered secret rejected
    with pytest.raises(CapabilityViolationError, match="is not registered in authoritative"):
        factory.create_grant(
            allowed_secret_refs=("unregistered_secret",),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
        )

    # 2. Factory enforces required_scope presence in granted_secret_scopes
    with pytest.raises(CapabilityViolationError, match="requires scope 'git_push'"):
        factory.create_grant(
            allowed_secret_refs=("github_token",),
            granted_secret_scopes=(SecretScope.API_INGRESS,),  # missing GIT_PUSH
        )


def test_capability_grant_factory_real_tools_dual_key_and_publication_coupling() -> None:
    sandbox_sec = RegisteredSecretDefinition(
        name="env_secret",
        source=SecretSource.ENV,
        source_key="OPENAI_API_KEY",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    registry = SecretRegistry([sandbox_sec])
    factory = CapabilityGrantFactory(secret_registry=registry)

    # 1. Dual-key dangerous shell check with real execute_bash tool
    with pytest.raises(CapabilityViolationError, match="Dangerous shell tool requested without"):
        factory.create_grant(
            allowed_tools=("execute_bash", "view_file"),
            allow_dangerous_shell=False,
        )

    with pytest.raises(
        CapabilityViolationError, match="allow_dangerous_shell=True granted without"
    ):
        factory.create_grant(
            allowed_tools=("view_file",),
            allow_dangerous_shell=True,
        )

    grant_shell = factory.create_grant(
        allowed_tools=("execute_bash", "view_file"),
        allow_dangerous_shell=True,
    )
    assert grant_shell.allow_dangerous_shell is True

    grant_safe = factory.create_grant(allowed_tools=("view_file",))
    assert grant_safe.allow_dangerous_shell is False

    # 2. Unknown tool rejection
    with pytest.raises(CapabilityViolationError, match="is not registered in ToolRegistry"):
        factory.create_grant(allowed_tools=("unknown_custom_tool",))

    # 3. Publication coupling check with real execute_github and execute_git tools
    # execute_github rejected when sandbox secret present
    with pytest.raises(CapabilityViolationError, match="automated external publication"):
        factory.create_grant(
            allowed_tools=("execute_github", "view_file"),
            allowed_secret_refs=("env_secret",),
            granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
        )
    # execute_git allowed with sandbox secret (local workspace write, not external publication)
    grant_git = factory.create_grant(
        allowed_tools=("execute_git", "view_file"),
        allowed_secret_refs=("env_secret",),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    assert grant_git.allowed_tools == ("execute_git", "view_file")
    # read-only view_file allowed with sandbox secret
    grant_read_only = factory.create_grant(
        allowed_tools=("view_file",),
        allowed_secret_refs=("env_secret",),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    assert grant_read_only.allowed_tools == ("view_file",)


def test_scratch_only_and_mandatory_denied_paths() -> None:
    factory = CapabilityGrantFactory()

    # Hostile grant: SCRATCH_ONLY with /workspace and empty denied_paths
    with pytest.raises(CapabilityViolationError, match="denied_paths must include mandatory"):
        factory.create_grant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace",),
            denied_paths=(),
        )

    # Hostile grant: SCRATCH_ONLY with /workspace even with valid denied_paths
    with pytest.raises(
        CapabilityViolationError, match="SCRATCH_ONLY filesystem policy only permits"
    ):
        factory.create_grant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace",),
            denied_paths=MANDATORY_DENIED_PATHS,
        )

    # Hostile grant: SCRATCH_ONLY with lexical path traversal ..
    with pytest.raises(CapabilityViolationError, match="forbidden|traversal|SCRATCH_ONLY"):
        factory.create_grant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace/.code-agent/scratch/../../etc",),
        )
    with pytest.raises(CapabilityViolationError, match="forbidden|traversal|SCRATCH_ONLY"):
        factory.create_grant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace/.code-agent/scratch/foo/../../../src/x",),
        )
    with pytest.raises(CapabilityViolationError, match="forbidden|traversal|SCRATCH_ONLY"):
        factory.create_grant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace/.code-agent/scratch/./foo",),
        )

    # Direct model validation for SCRATCH_ONLY
    with pytest.raises(ValidationError, match="SCRATCH_ONLY filesystem policy only permits"):
        SandboxCapabilityGrant(
            filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
            allowed_paths=("/workspace/src",),
            denied_paths=MANDATORY_DENIED_PATHS,
        )

    # Valid scratch paths
    grant_scratch = factory.create_grant(
        filesystem=FileSystemAccessPolicy.SCRATCH_ONLY,
        allowed_paths=(SCRATCH_PATH_PREFIX, f"{SCRATCH_PATH_PREFIX}/temp"),
    )
    assert grant_scratch.filesystem == FileSystemAccessPolicy.SCRATCH_ONLY
    assert len(grant_scratch.allowed_paths) == 2


def test_secret_resolver_dual_key_and_broker_resolution() -> None:
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
    factory = CapabilityGrantFactory(secret_registry=registry)

    env = {"GH_TOKEN": "gh-secret-token"}
    store = {"openai_key_store": "sk-openai-token"}
    resolver = SecretResolver(registry, env=env, secret_store=store)

    redactor = SecretRedactor()

    # Valid sandbox resolution
    grant_sandbox = factory.create_grant(
        allowed_secret_refs=(SecretRef(name="openai_key"),),
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
    grant_broker = factory.create_grant(
        allowed_secret_refs=("github_token",),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    with pytest.raises(BrokerOnlySecretExposureError, match="BROKER_ONLY"):
        resolver.resolve_for_sandbox(SecretRef(name="github_token"), grant_broker)

    # Valid broker resolution
    resolved_broker = resolver.resolve_for_broker(SecretRef(name="github_token"), grant_broker)
    assert isinstance(resolved_broker, ResolvedSecret)
    assert resolved_broker.reveal_secret_value() == "gh-secret-token"

    # String name broker resolution (canonical registry lookup)
    resolved_broker_str = resolver.resolve_for_broker("github_token", grant_broker)
    assert resolved_broker_str.reveal_secret_value() == "gh-secret-token"

    with pytest.raises(SecretNotFoundError, match="not found in registry"):
        resolver.resolve_for_broker("unregistered_token", grant_broker)

    # Dual-key authorization failures
    # 1. Missing name in allowed_secret_refs
    grant_no_name = factory.create_grant(
        allowed_secret_refs=(),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    with pytest.raises(UnauthorizedSecretError, match="not in grant.allowed_secret_refs"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_no_name)

    # 2. Direct grant lacking scope
    grant_no_scope = SandboxCapabilityGrant(
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=frozenset([SecretScope.GIT_PUSH]),
    )
    with pytest.raises(MissingSecretScopeError, match="Grant lacks required scope"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_no_scope)


def test_secret_resolver_file_source_and_registry_edge_cases() -> None:
    # FILE source key traversal rejection
    with pytest.raises(ValidationError, match="traversal|relative path"):
        RegisteredSecretDefinition(
            name="bad_file_sec",
            source=SecretSource.FILE,
            source_key="../../etc/passwd",
            required_scope=SecretScope.CUSTOM,
            exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
            destination_mount_path="bad.pem",
        )
    with pytest.raises(ValidationError, match="relative path"):
        RegisteredSecretDefinition(
            name="abs_file_sec",
            source=SecretSource.FILE,
            source_key="/etc/passwd",
            required_scope=SecretScope.CUSTOM,
            exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
            destination_mount_path="bad.pem",
        )

    file_sec = RegisteredSecretDefinition(
        name="ca_cert",
        source=SecretSource.FILE,
        source_key="custom_ca.pem",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
        destination_mount_path="custom_ca.pem",
    )
    # Serialization round-trip test
    dumped = file_sec.model_dump(mode="json")
    reloaded = RegisteredSecretDefinition.model_validate(dumped)
    assert reloaded.destination_mount_path == "/run/secrets/code-agent/custom_ca.pem"
    assert reloaded.name == "ca_cert"

    registry = SecretRegistry([file_sec])
    factory = CapabilityGrantFactory(secret_registry=registry)

    # Registry duplicate
    with pytest.raises(ValueError, match="already registered"):
        registry.register(file_sec)

    # Registry get None and require missing
    assert registry.get("missing_key") is None
    with pytest.raises(SecretNotFoundError, match="not found in registry"):
        registry.require("missing_key")

    # File store resolution
    resolver = SecretResolver(registry, file_store={"custom_ca.pem": "CERT-CONTENT-XYZ"})
    grant = factory.create_grant(
        allowed_secret_refs=("ca_cert",),
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
    with pytest.raises(SecretNotFoundError, match="not found in source"):
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
    factory = CapabilityGrantFactory(secret_registry=SecretRegistry([sec]))
    with pytest.raises(CapabilityViolationError, match="requires non-empty allowed_egress_hosts"):
        factory.create_grant(
            network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
            allowed_secret_refs=("github_token",),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=(),
        )

    with pytest.raises(CapabilityViolationError, match="DISABLED network cannot specify"):
        factory.create_grant(
            network=NetworkEgressPolicy.DISABLED,
            allowed_secret_refs=("github_token",),
            granted_secret_scopes=(SecretScope.GIT_PUSH,),
            allowed_egress_hosts=("api.github.com",),
        )


def test_consumption_side_grant_validation_and_resolver_invariants() -> None:
    sec = RegisteredSecretDefinition(
        name="openai_key",
        source=SecretSource.SECRET_STORE,
        source_key="openai_key_store",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        permitted_egress_hosts=("api.openai.com",),
        destination_env_var="OPENAI_API_KEY",
    )
    registry = SecretRegistry([sec])
    resolver = SecretResolver(registry, secret_store={"openai_key_store": "sk-real-val"})

    # 1. Hand-crafted grant with PUBLIC_HTTPS_PROXY is rejected by resolver
    grant_proxy = SandboxCapabilityGrant(
        network=NetworkEgressPolicy.PUBLIC_HTTPS_PROXY,
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
    )
    with pytest.raises(CapabilityViolationError, match="PUBLIC_HTTPS_PROXY network is forbidden"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_proxy)

    # 2. Hand-crafted grant with exceeding allowed_egress_hosts is rejected by resolver
    grant_bad_hosts = SandboxCapabilityGrant(
        network=NetworkEgressPolicy.ALLOWLISTED_HOSTS,
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
        allowed_egress_hosts=("evil.attacker.com",),
    )
    with pytest.raises(CapabilityViolationError, match="exceed.*permitted"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_bad_hosts)

    # 3. Hand-crafted grant combining sandbox secret with publication tool is rejected
    grant_pub = SandboxCapabilityGrant(
        allowed_tools=("execute_github",),
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
    )
    with pytest.raises(CapabilityViolationError, match="publication tools"):
        resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_pub)

    # 4. validate_grant_for_execution rejects hand-crafted invalid grants
    with pytest.raises(CapabilityViolationError, match="PUBLIC_HTTPS_PROXY is forbidden"):
        validate_grant_for_execution(grant_proxy, secret_registry=registry)

    grant_dangerous_shell = SandboxCapabilityGrant(
        allowed_tools=("execute_bash",),
        allow_dangerous_shell=False,
    )
    with pytest.raises(CapabilityViolationError, match="Dangerous shell tool"):
        validate_grant_for_execution(grant_dangerous_shell, secret_registry=registry)

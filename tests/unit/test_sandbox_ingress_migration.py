"""Unit tests for ingress migration, authoritative registry, and ephemeral store."""

from __future__ import annotations

import pytest

from sandbox.capability import (
    BrokerOnlySecretExposureError,
    CapabilityGrantFactory,
    CapabilityViolationError,
    DeprecatedLegacySecretsError,
    EphemeralSecretHandle,
    EphemeralSecretRecord,
    IngressMigrationAdapter,
    InMemoryEphemeralSecretStore,
    MissingTaskContextError,
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretNotFoundError,
    SecretRef,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
    create_authoritative_secret_registry,
    sanitize_legacy_ingress_payload,
    validate_secret_refs,
)
from workers.base import WorkerRequest


def test_sanitize_legacy_ingress_payload_malformed_input_sanitization() -> None:
    sentinel_secret = "super-secret-token-xyz-123"

    # 1. Plain ingress sanitizer rejects non-dict payload without echoing secret
    with pytest.raises(ValueError) as exc_non_dict:
        sanitize_legacy_ingress_payload(sentinel_secret)
    assert sentinel_secret not in str(exc_non_dict.value)
    assert sentinel_secret not in repr(exc_non_dict.value)

    # 2. Plain ingress sanitizer rejects non-dict secrets payload without echoing secret
    with pytest.raises(ValueError) as exc_str:
        sanitize_legacy_ingress_payload({"task_text": "Run task", "secrets": sentinel_secret})
    assert sentinel_secret not in str(exc_str.value)
    assert sentinel_secret not in repr(exc_str.value)

    # 3. Plain ingress sanitizer rejects non-dict list secrets payload without echoing secret
    with pytest.raises(ValueError) as exc_list:
        sanitize_legacy_ingress_payload({"task_text": "Run task", "secrets": [sentinel_secret]})
    assert sentinel_secret not in str(exc_list.value)
    assert sentinel_secret not in repr(exc_list.value)

    # 4. Valid ingress payload sanitizes values
    sanitized = sanitize_legacy_ingress_payload(
        {"task_text": "Run task", "secrets": {"github_token": sentinel_secret}}
    )
    assert sanitized["secrets"] == {"github_token": "[REDACTED_AT_INGRESS]"}
    assert sentinel_secret not in str(sanitized)


def test_ingress_migration_adapter_and_worker_request_migration() -> None:
    secret_value = "super-secret-token-xyz"

    # IngressMigrationAdapter.adapt rejects legacy secrets fail-closed
    with pytest.raises(DeprecatedLegacySecretsError, match="no longer accepted"):
        IngressMigrationAdapter.adapt(secrets={"github_token": secret_value})

    # Valid secret_refs succeed
    refs = IngressMigrationAdapter.adapt(
        secret_refs=(SecretRef(name="github_token"),),
    )
    assert refs == (SecretRef(name="github_token"),)

    # WorkerRequest legacy migration: populates secret_refs while
    # retaining in-memory secrets for execution
    worker_req = WorkerRequest(
        task_text="Run worker task",
        secrets={"github_token": secret_value},
    )
    assert worker_req.secret_refs == (SecretRef(name="github_token"),)
    assert worker_req.secrets == {"github_token": secret_value}
    assert secret_value not in worker_req.model_dump_json()
    assert secret_value not in repr(worker_req)

    # Raw JSON dictionary secret_refs mixed with legacy secrets
    mixed_req = WorkerRequest.model_validate(
        {
            "task_text": "Run task",
            "secret_refs": [{"name": "modern_token"}],
            "secrets": {"legacy_token": "legacy_val"},
        }
    )
    assert len(mixed_req.secret_refs) == 2
    assert {r.name for r in mixed_req.secret_refs} == {"modern_token", "legacy_token"}
    assert mixed_req.secrets == {"legacy_token": "legacy_val"}


def test_create_authoritative_secret_registry_and_validate_secret_refs() -> None:
    environ = {
        "OPENAI_API_KEY": "sk-mock-123",
        "GITHUB_TOKEN": "ghp_mock_123",
        "CODE_AGENT_REGISTERED_SECRETS": "custom_token,team_cert",
    }
    registry = create_authoritative_secret_registry(environ)

    # Standard definitions are present
    assert "github_token" in registry
    assert "gh_token" in registry
    assert "openai_api_key" in registry
    assert "gemini_api_key" in registry
    assert "openrouter_api_key" in registry
    assert "custom_token" in registry
    assert "team_cert" in registry

    gh_def = registry.require("github_token")
    assert gh_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY
    assert gh_def.required_scope == SecretScope.GIT_PUSH

    # Validation of valid secret references
    valid_refs = (SecretRef(name="github_token"), SecretRef(name="openai_api_key"))
    validate_secret_refs(valid_refs, registry=registry)

    # Unregistered secret reference fails closed
    with pytest.raises(SecretNotFoundError, match="unregistered_key"):
        validate_secret_refs(
            (SecretRef(name="github_token"), SecretRef(name="unregistered_key")),
            registry=registry,
        )

    # Metadata validation fails closed by default
    ref_with_meta = SecretRef(name="github_token", metadata=(("k", "v"),))
    with pytest.raises(CapabilityViolationError, match="metadata"):
        validate_secret_refs((ref_with_meta,), registry=registry, allow_metadata=False)

    # Metadata allowed when explicitly permitted
    validate_secret_refs((ref_with_meta,), registry=registry, allow_metadata=True)


def test_ephemeral_secret_store_operations() -> None:
    store = InMemoryEphemeralSecretStore(
        {
            "initial_key": EphemeralSecretRecord(
                handle_id="initial_key",
                task_id="task_init",
                value="val1",
                required_scope=SecretScope.CUSTOM,
            )
        }
    )
    assert len(store) == 1
    assert "initial_key" in store
    assert store.get("initial_key", task_id="task_init") == "val1"
    assert store.get("initial_key") is None  # Missing task_id fails closed
    assert store.get("initial_key", task_id="wrong_task") is None

    store.store("new_key", "val2", task_id="task_init")
    assert store.get("new_key", task_id="task_init") == "val2"
    assert store.has("new_key", task_id="task_init") is True
    assert store.has("new_key") is False

    store.remove("initial_key", task_id="wrong_task")
    assert store.has("initial_key", task_id="task_init") is True
    store.remove("initial_key", task_id="task_init")
    assert store.has("initial_key", task_id="task_init") is False

    handle = EphemeralSecretHandle(handle_id="ephem_handle_123")
    assert handle.handle_id == "ephem_handle_123"


def test_ephemeral_store_registry_resolution_and_sandbox_execution() -> None:
    ephem_store = InMemoryEphemeralSecretStore()
    record = EphemeralSecretRecord(
        handle_id="ephem_CUSTOM_TOKEN_123",
        task_id="task_123",
        value="caller_secret_xyz_999",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="CODE_AGENT_SECRET_CUSTOM_TOKEN",
    )
    ephem_store.store_record(record)

    registry = SecretRegistry(ephemeral_store=ephem_store, task_id="task_123")
    sec_def = registry.require("ephem_CUSTOM_TOKEN_123")
    assert sec_def.source == SecretSource.EPHEMERAL
    assert sec_def.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"

    factory = CapabilityGrantFactory(secret_registry=registry)
    grant = factory.create_grant(
        allowed_secret_refs=("ephem_CUSTOM_TOKEN_123",),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    resolver = SecretResolver(registry, task_id="task_123", ephemeral_store=ephem_store)
    resolved = resolver.resolve_for_sandbox(SecretRef(name="ephem_CUSTOM_TOKEN_123"), grant)
    assert resolved.name == "ephem_CUSTOM_TOKEN_123"
    assert resolved.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"
    assert resolved.reveal_secret_value() == "caller_secret_xyz_999"


def test_ephemeral_migration_prevents_shadowing_authoritative_broker_secrets() -> None:
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )

    ephem_store = InMemoryEphemeralSecretStore()
    record = EphemeralSecretRecord(
        handle_id="ephem_openai_api_key_456",
        task_id="task_shadow",
        value="caller_provided_openai_key_val",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    ephem_store.store_record(record)

    shadow_registry = SecretRegistry(
        registry,
        ephemeral_store=ephem_store,
        task_id="task_shadow",
    )

    auth_def = shadow_registry.require("openai_key")
    assert auth_def.source == SecretSource.ENV
    assert auth_def.source_key == "OPENAI_API_KEY"

    ephem_def = shadow_registry.require("ephem_openai_api_key_456")
    assert ephem_def.source == SecretSource.EPHEMERAL
    assert ephem_def.destination_env_var == "OPENAI_API_KEY"

    resolver = SecretResolver(
        shadow_registry,
        task_id="task_shadow",
        env={"OPENAI_API_KEY": "broker_system_key_123"},
        ephemeral_store=ephem_store,
    )
    grant_caller = CapabilityGrantFactory(secret_registry=shadow_registry).create_grant(
        allowed_secret_refs=("ephem_openai_api_key_456",),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    res_caller = resolver.resolve_for_sandbox(
        SecretRef(name="ephem_openai_api_key_456"), grant_caller
    )
    assert res_caller.reveal_secret_value() == "caller_provided_openai_key_val"

    grant_broker = CapabilityGrantFactory(secret_registry=shadow_registry).create_grant(
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    res_broker = resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_broker)
    assert res_broker.reveal_secret_value() == "broker_system_key_123"


def test_ephemeral_reserved_github_token_enforces_broker_only() -> None:
    ephem_store = InMemoryEphemeralSecretStore()
    record = EphemeralSecretRecord(
        handle_id="ephem_github_token_789",
        task_id="task_gh",
        value="ghp_caller_custom_token_12345",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
    )
    ephem_store.store_record(record)

    task_registry = SecretRegistry(ephemeral_store=ephem_store, task_id="task_gh")
    sec_def = task_registry.require("ephem_github_token_789")

    # 1. Enforces BROKER_ONLY and GIT_PUSH scope
    assert sec_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY
    assert sec_def.required_scope == SecretScope.GIT_PUSH
    assert sec_def.destination_env_var is None

    # 2. Resolving for sandbox fails closed
    factory = CapabilityGrantFactory(secret_registry=task_registry)
    grant = factory.create_grant(
        allowed_secret_refs=("ephem_github_token_789",),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    resolver = SecretResolver(task_registry, task_id="task_gh", ephemeral_store=ephem_store)
    with pytest.raises(BrokerOnlySecretExposureError, match="BROKER_ONLY"):
        resolver.resolve_for_sandbox(SecretRef(name="ephem_github_token_789"), grant)

    # 3. Resolving for broker succeeds
    broker_res = resolver.resolve_for_broker(SecretRef(name="ephem_github_token_789"), grant)
    assert broker_res.reveal_secret_value() == "ghp_caller_custom_token_12345"


def test_ephemeral_secret_missing_task_id_fails_closed() -> None:
    store = InMemoryEphemeralSecretStore()
    record = EphemeralSecretRecord(
        handle_id="ephem_CUSTOM_KEY_001",
        task_id="task_owner_1",
        value="secret_abc",
        required_scope=SecretScope.CUSTOM,
        destination_env_var="CODE_AGENT_SECRET_CUSTOM_KEY",
    )
    store.store_record(record)

    # 1. EphemeralSecretStore requires task_id matching owner
    assert store.get_record("ephem_CUSTOM_KEY_001") is None
    assert store.get_record("ephem_CUSTOM_KEY_001", task_id="wrong_task") is None
    assert store.get_record("ephem_CUSTOM_KEY_001", task_id="task_owner_1") is not None

    # 2. SecretRegistry without task_id cannot resolve ephemeral secret
    unscoped_registry = SecretRegistry(ephemeral_store=store)
    assert unscoped_registry.get("ephem_CUSTOM_KEY_001") is None
    with pytest.raises(SecretNotFoundError):
        unscoped_registry.require("ephem_CUSTOM_KEY_001")

    # 3. SecretResolver without task_id raises MissingTaskContextError
    scoped_registry = SecretRegistry(ephemeral_store=store, task_id="task_owner_1")
    factory = CapabilityGrantFactory(secret_registry=scoped_registry)
    grant = factory.create_grant(
        allowed_secret_refs=("ephem_CUSTOM_KEY_001",),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    unscoped_resolver = SecretResolver(scoped_registry, ephemeral_store=store)
    with pytest.raises(MissingTaskContextError, match="Missing task_id"):
        unscoped_resolver.resolve_for_sandbox(SecretRef(name="ephem_CUSTOM_KEY_001"), grant)


def test_destination_collision_validation_fails_closed() -> None:
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_key_sys",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )
    registry.register(
        RegisteredSecretDefinition(
            name="openai_key_user",
            source=SecretSource.ENV,
            source_key="USER_OPENAI_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )

    factory = CapabilityGrantFactory(secret_registry=registry)
    with pytest.raises(CapabilityViolationError, match="Conflicting sandbox env destination"):
        factory.create_grant(
            allowed_secret_refs=("openai_key_sys", "openai_key_user"),
            granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
        )

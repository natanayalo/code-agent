"""Unit tests for legacy ingress migration, sanitization, and ephemeral store."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox.capability import (
    BrokerOnlySecretExposureError,
    CapabilityGrantFactory,
    CapabilityViolationError,
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    EphemeralSecretHandle,
    EphemeralSecretRecord,
    EphemeralSecretStore,
    IngressMigrationAdapter,
    InMemoryEphemeralSecretStore,
    LegacyIngressTaskRequest,
    MissingTaskContextError,
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretNotFoundError,
    SecretRef,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
    sanitize_legacy_ingress_payload,
)
from workers.base import WorkerRequest


def test_legacy_ingress_task_request_malformed_input_sanitization() -> None:
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

    # 3. from_raw_payload with non-dict secrets
    with pytest.raises(ValueError) as exc_raw:
        LegacyIngressTaskRequest.from_raw_payload(
            {"task_text": "Run task", "secrets": [sentinel_secret]}
        )
    assert sentinel_secret not in str(exc_raw.value)
    assert sentinel_secret not in repr(exc_raw.value)

    # 4. Valid ingress payload sanitizes values
    valid_req = LegacyIngressTaskRequest.from_raw_payload(
        {"task_text": "Run task", "secrets": {"github_token": sentinel_secret}}
    )
    assert valid_req.secrets == {"github_token": "[REDACTED_AT_INGRESS]"}
    assert sentinel_secret not in valid_req.model_dump_json()


def test_ingress_migration_adapter_and_worker_request_migration() -> None:
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


def test_ephemeral_secret_store_and_ingress_migration_lifecycle() -> None:
    # 1. EphemeralSecretStore / InMemoryEphemeralSecretStore basic operations
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

    # 2. Transactional validation: invalid request does not commit secrets
    ephem_store = EphemeralSecretStore()
    registry = SecretRegistry()
    with pytest.raises(ValidationError):
        IngressMigrationAdapter.adapt_and_register_ephemeral(
            {"task_text": "", "secrets": {"ORPHAN_SECRET": "secret_abc"}},
            registry=registry,
            ephemeral_store=ephem_store,
            task_id="task_123",
        )
    assert len(ephem_store) == 0
    assert len(registry) == 0

    # 3. Adapt and register ephemeral creates opaque references and preserves destination
    raw_payload = {
        "task_text": "Run ephemeral secret task",
        "secrets": {"CUSTOM-TOKEN": "caller_secret_xyz_999"},
    }
    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=ephem_store,
        task_id="task_123",
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    assert len(refs) == 1
    opaque_ref = refs[0]
    assert opaque_ref.name.startswith("ephem_CUSTOM_TOKEN_")
    assert ephem_store.get(opaque_ref.name, task_id="task_123") == "caller_secret_xyz_999"

    sec_def = registry.require(opaque_ref.name, task_id="task_123")
    assert sec_def.source == SecretSource.EPHEMERAL
    assert sec_def.source_key == opaque_ref.name
    assert sec_def.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"

    factory = CapabilityGrantFactory(secret_registry=registry)
    grant = factory.create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    resolver = SecretResolver(registry, task_id="task_123", ephemeral_store=ephem_store)
    resolved = resolver.resolve_for_sandbox(opaque_ref, grant)
    assert resolved.name == opaque_ref.name
    assert resolved.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"
    assert resolved.reveal_secret_value() == "caller_secret_xyz_999"


def test_ephemeral_migration_prevents_shadowing_authoritative_broker_secrets() -> None:
    # 1. Pre-populate authoritative broker secret
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

    # 2. Legacy caller passes secrets with same name
    raw_payload = {
        "task_text": "Run task with custom openai key",
        "secrets": {"openai_api_key": "caller_provided_openai_key_val"},
    }
    ephem_store = EphemeralSecretStore()
    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=ephem_store,
        task_id="task_shadow",
        scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )

    # 3. Opaque ref is distinct from authoritative name
    assert len(refs) == 1
    opaque_ref = refs[0]
    assert opaque_ref.name != "openai_key"
    assert opaque_ref.name.startswith("ephem_openai_api_key_")

    # 4. Authoritative broker secret is preserved untouched
    auth_def = registry.require("openai_key")
    assert auth_def.source == SecretSource.ENV
    assert auth_def.source_key == "OPENAI_API_KEY"

    # 5. Resolving opaque ref resolves caller's ephemeral material
    resolver = SecretResolver(
        registry,
        task_id="task_shadow",
        env={"OPENAI_API_KEY": "broker_system_key_123"},
        ephemeral_store=ephem_store,
    )
    grant_caller = CapabilityGrantFactory(secret_registry=registry).create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    res_caller = resolver.resolve_for_sandbox(opaque_ref, grant_caller)
    assert res_caller.destination_env_var == "OPENAI_API_KEY"
    assert res_caller.reveal_secret_value() == "caller_provided_openai_key_val"

    # 6. Resolving broker ref resolves broker's env key
    grant_broker = CapabilityGrantFactory(secret_registry=registry).create_grant(
        allowed_secret_refs=("openai_key",),
        granted_secret_scopes=(SecretScope.PROVIDER_AUTH,),
    )
    res_broker = resolver.resolve_for_sandbox(SecretRef(name="openai_key"), grant_broker)
    assert res_broker.reveal_secret_value() == "broker_system_key_123"


def test_ephemeral_migration_sandbox_file_destination_mount_path() -> None:
    registry = SecretRegistry()
    ephem_store = EphemeralSecretStore()
    raw_payload = {
        "task_text": "Run file secret task",
        "secrets": {"custom_cert": "-----BEGIN CERTIFICATE-----..."},
    }
    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=ephem_store,
        task_id="task_file",
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
    )
    assert len(refs) == 1
    sec_def = registry.require(refs[0].name, task_id="task_file")
    assert sec_def.exposure_policy == SecretExposurePolicy.SANDBOX_FILE
    assert sec_def.destination_mount_path == "/run/secrets/code-agent/ephemeral_custom_cert.secret"
    assert sec_def.destination_mount_name == "ephemeral_custom_cert.secret"
    assert sec_def.destination_env_var is None


def test_ephemeral_migration_reserved_github_token_enforces_broker_only() -> None:
    registry = SecretRegistry()
    ephem_store = EphemeralSecretStore()
    raw_payload = {
        "task_text": "Run github task",
        "secrets": {"github_token": "ghp_caller_custom_token_12345"},
    }
    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=ephem_store,
        task_id="task_gh",
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    assert len(refs) == 1
    opaque_ref = refs[0]
    sec_def = registry.require(opaque_ref.name, task_id="task_gh")

    # 1. Enforces BROKER_ONLY and GIT_PUSH scope
    assert sec_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY
    assert sec_def.required_scope == SecretScope.GIT_PUSH
    assert sec_def.destination_env_var is None

    # 2. Resolving for sandbox fails closed
    factory = CapabilityGrantFactory(secret_registry=registry)
    grant = factory.create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.GIT_PUSH,),
    )
    resolver = SecretResolver(registry, task_id="task_gh", ephemeral_store=ephem_store)
    with pytest.raises(BrokerOnlySecretExposureError, match="BROKER_ONLY"):
        resolver.resolve_for_sandbox(opaque_ref, grant)

    # 3. Resolving for broker succeeds
    broker_res = resolver.resolve_for_broker(opaque_ref, grant)
    assert broker_res.reveal_secret_value() == "ghp_caller_custom_token_12345"


def test_distributed_secret_definition_resolution_across_processes() -> None:
    # 1. Ingress process adapts and registers record into shared store
    shared_store = InMemoryEphemeralSecretStore()
    ingress_registry = SecretRegistry(ephemeral_store=shared_store, task_id="task_abc_123")
    raw_payload = {
        "task_text": "Distributed execution task",
        "secrets": {"custom_api_key": "caller_secret_val_999"},
    }
    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=ingress_registry,
        ephemeral_store=shared_store,
        task_id="task_abc_123",
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    assert len(refs) == 1
    opaque_ref = refs[0]

    # 2. Worker process in separate memory space with fresh SecretRegistry
    worker_registry = SecretRegistry(ephemeral_store=shared_store, task_id="task_abc_123")
    assert opaque_ref.name not in worker_registry._definitions

    # Dynamic lookup retrieves authoritative definition from shared store
    def_on_worker = worker_registry.require(opaque_ref.name)
    assert def_on_worker.name == opaque_ref.name
    assert def_on_worker.required_scope == SecretScope.CUSTOM
    assert def_on_worker.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_API_KEY"

    # Worker factory and resolver execute successfully
    worker_factory = CapabilityGrantFactory(secret_registry=worker_registry)
    grant = worker_factory.create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    worker_resolver = SecretResolver(
        worker_registry, task_id="task_abc_123", ephemeral_store=shared_store
    )
    res = worker_resolver.resolve_for_sandbox(opaque_ref, grant)
    assert res.reveal_secret_value() == "caller_secret_val_999"

    # 3. Wrong task_id cannot resolve the record
    wrong_task_registry = SecretRegistry(ephemeral_store=shared_store, task_id="wrong_task_999")
    assert wrong_task_registry.get(opaque_ref.name) is None


def test_ephemeral_secret_missing_task_id_fails_closed() -> None:
    store = EphemeralSecretStore()
    registry = SecretRegistry(ephemeral_store=store)
    raw_payload = {
        "task_text": "Run task",
        "secrets": {"CUSTOM_KEY": "secret_abc"},
    }
    # adapt_and_register_ephemeral requires non-empty task_id
    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        IngressMigrationAdapter.adapt_and_register_ephemeral(
            raw_payload,
            registry=registry,
            ephemeral_store=store,
            task_id="",
        )

    refs = IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=store,
        task_id="task_owner_1",
    )
    opaque_ref = refs[0]

    # 1. EphemeralSecretStore requires task_id matching owner
    assert store.get_record(opaque_ref.name) is None
    assert store.get_record(opaque_ref.name, task_id="wrong_task") is None
    assert store.get_record(opaque_ref.name, task_id="task_owner_1") is not None

    # 2. SecretRegistry without task_id cannot resolve ephemeral secret
    unscoped_registry = SecretRegistry(ephemeral_store=store)
    assert unscoped_registry.get(opaque_ref.name) is None
    with pytest.raises(SecretNotFoundError):
        unscoped_registry.require(opaque_ref.name)

    # 3. SecretResolver without task_id raises MissingTaskContextError
    scoped_registry = SecretRegistry(ephemeral_store=store, task_id="task_owner_1")
    factory = CapabilityGrantFactory(secret_registry=scoped_registry)
    grant = factory.create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    unscoped_resolver = SecretResolver(scoped_registry, ephemeral_store=store)
    with pytest.raises(MissingTaskContextError, match="Missing task_id"):
        unscoped_resolver.resolve_for_sandbox(opaque_ref, grant)


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

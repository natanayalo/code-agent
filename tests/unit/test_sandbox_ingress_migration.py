"""Unit tests for legacy ingress migration, sanitization, and ephemeral store."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sandbox.capability import (
    CapabilityGrantFactory,
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    EphemeralSecretHandle,
    EphemeralSecretStore,
    IngressMigrationAdapter,
    InMemoryEphemeralSecretStore,
    LegacyIngressTaskRequest,
    RegisteredSecretDefinition,
    SecretExposurePolicy,
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
    store = InMemoryEphemeralSecretStore({"initial_key": "val1"})
    assert len(store) == 1
    assert "initial_key" in store
    assert store.get("initial_key") == "val1"
    store.store("new_key", "val2")
    assert store.get("new_key") == "val2"
    assert store.has("new_key") is True
    store.remove("initial_key")
    assert "initial_key" not in store

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
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    assert len(refs) == 1
    opaque_ref = refs[0]
    assert opaque_ref.name.startswith("ephem_CUSTOM_TOKEN_")
    assert ephem_store.get(opaque_ref.name) == "caller_secret_xyz_999"

    sec_def = registry.require(opaque_ref.name)
    assert sec_def.source == SecretSource.EPHEMERAL
    assert sec_def.source_key == opaque_ref.name
    assert sec_def.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"

    factory = CapabilityGrantFactory(secret_registry=registry)
    grant = factory.create_grant(
        allowed_secret_refs=(opaque_ref.name,),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    resolver = SecretResolver(registry, ephemeral_store=ephem_store)
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
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
    )
    assert len(refs) == 1
    sec_def = registry.require(refs[0].name)
    assert sec_def.exposure_policy == SecretExposurePolicy.SANDBOX_FILE
    assert sec_def.destination_mount_path == "/run/secrets/code-agent/ephemeral_custom_cert.secret"
    assert sec_def.destination_mount_name == "ephemeral_custom_cert.secret"
    assert sec_def.destination_env_var is None

"""Unit tests for legacy ingress migration, sanitization, and ephemeral store."""

from __future__ import annotations

import pytest

from sandbox.capability import (
    CapabilityGrantFactory,
    ConflictingSecretDeclarationError,
    DeprecatedLegacySecretsError,
    EphemeralSecretStore,
    IngressMigrationAdapter,
    LegacyIngressTaskRequest,
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
    # 1. EphemeralSecretStore basic operations
    store = EphemeralSecretStore({"initial_key": "val1"})
    assert len(store) == 1
    assert "initial_key" in store
    assert store.get("initial_key") == "val1"
    store.store("new_key", "val2")
    assert store.get("new_key") == "val2"
    assert store.has("new_key") is True
    store.remove("initial_key")
    assert "initial_key" not in store

    # 2. Ingress migration preserves raw value in ephemeral store while request only carries ref
    raw_payload = {
        "task_text": "Run ephemeral secret task",
        "secrets": {"CUSTOM_TOKEN": "caller_secret_xyz_999"},
    }
    ephem_store = EphemeralSecretStore()
    refs = IngressMigrationAdapter.adapt(raw_payload, ephemeral_store=ephem_store)
    assert refs == (SecretRef(name="CUSTOM_TOKEN"),)
    assert ephem_store.get("CUSTOM_TOKEN") == "caller_secret_xyz_999"

    # 3. Adapt and register ephemeral in registry for end-to-end resolution
    registry = SecretRegistry()
    IngressMigrationAdapter.adapt_and_register_ephemeral(
        raw_payload,
        registry=registry,
        ephemeral_store=ephem_store,
        scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    assert registry.get("CUSTOM_TOKEN") is not None
    assert registry.require("CUSTOM_TOKEN").source == SecretSource.EPHEMERAL

    factory = CapabilityGrantFactory(secret_registry=registry)
    grant = factory.create_grant(
        allowed_secret_refs=("CUSTOM_TOKEN",),
        granted_secret_scopes=(SecretScope.CUSTOM,),
    )
    resolver = SecretResolver(registry, ephemeral_store=ephem_store)
    resolved = resolver.resolve_for_sandbox(SecretRef(name="CUSTOM_TOKEN"), grant)
    assert resolved.name == "CUSTOM_TOKEN"
    assert resolved.reveal_secret_value() == "caller_secret_xyz_999"

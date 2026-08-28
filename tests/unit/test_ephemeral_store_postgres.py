"""Unit tests for the Postgres-backed ephemeral secret store."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from db.models import EphemeralSecret
from sandbox.ephemeral_store_postgres import (
    PostgresEphemeralSecretStore,
    SessionFactoryEphemeralSecretStore,
)
from sandbox.secrets import EphemeralSecretRecord, SecretExposurePolicy, SecretScope


@pytest.fixture
def mock_session():
    return MagicMock(spec=Session)


@pytest.fixture
def mock_fernet():
    fernet = MagicMock()
    fernet.encrypt.return_value = b"encrypted_stuff"
    fernet.decrypt.return_value = b"decrypted_value"
    return fernet


@pytest.fixture
def mock_encrypted_json(mock_fernet):
    with patch("sandbox.ephemeral_store_postgres.EncryptedJSON") as mock_cls:
        instance = mock_cls.return_value
        instance.is_active.return_value = True
        instance.fernet = mock_fernet
        yield instance


def test_postgres_store_record_requires_active_key(mock_session, mock_encrypted_json):
    mock_encrypted_json.is_active.return_value = False
    store = PostgresEphemeralSecretStore(mock_session)
    record = EphemeralSecretRecord(
        handle_id="test_key",
        task_id="test_task",
        value="test_value",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    with pytest.raises(RuntimeError, match="requires an active encryption key"):
        store.store_record(record)


def test_postgres_store_record_requires_fernet(mock_session, mock_encrypted_json):
    mock_encrypted_json.fernet = None
    store = PostgresEphemeralSecretStore(mock_session)
    record = EphemeralSecretRecord(
        handle_id="test_key",
        task_id="test_task",
        value="test_value",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )
    with pytest.raises(RuntimeError, match="Fernet key not available"):
        store.store_record(record)


def test_postgres_store_record_updates_existing(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    record = EphemeralSecretRecord(
        handle_id="test_key",
        task_id="test_task",
        value="test_value",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    mock_filter.first.return_value = existing_secret

    handle = store.store_record(record)

    assert handle == "test_key"
    assert existing_secret.encrypted_value == b"encrypted_stuff"
    assert "required_scope" in existing_secret.metadata_payload
    mock_session.add.assert_not_called()
    mock_session.flush.assert_called_once()


def test_postgres_store_helper(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    mock_filter.first.return_value = None

    handle = store.store("my_key", "my_val", task_id="task_123")

    assert handle == "my_key"
    mock_session.add.assert_called_once()


def test_postgres_store_helper_empty_task_id(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        store.store("my_key", "my_val", task_id="")


def test_postgres_get_record_no_task_id(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    assert store.get_record("my_key") is None


def test_postgres_get_record_not_active(mock_session, mock_encrypted_json):
    mock_encrypted_json.is_active.return_value = False
    store = PostgresEphemeralSecretStore(mock_session)
    with pytest.raises(RuntimeError, match="requires an active encryption key"):
        store.get_record("my_key", task_id="task_123")


def test_postgres_get_record_no_fernet(mock_session, mock_encrypted_json):
    mock_encrypted_json.fernet = None
    store = PostgresEphemeralSecretStore(mock_session)
    with pytest.raises(RuntimeError, match="Fernet key not available"):
        store.get_record("my_key", task_id="task_123")


def test_postgres_get_record_not_found(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    mock_filter.first.return_value = None

    assert store.get_record("my_key", task_id="task_123") is None


def test_postgres_get_record_expired(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) - timedelta(hours=1)
    mock_filter.first.return_value = existing_secret

    assert store.get_record("my_key", task_id="task_123") is None
    mock_session.delete.assert_called_once_with(existing_secret)
    mock_session.flush.assert_called_once()


def test_postgres_get_record_success(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    record = store.get_record("my_key", task_id="task_123")
    assert record is not None
    assert record.value == "decrypted_value"
    assert record.handle_id == "my_key"


def test_postgres_remove_no_task_id(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    store.remove("my_key")  # Should do nothing


def test_postgres_remove_not_found(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    mock_filter.first.return_value = None
    store.remove("my_key", task_id="task_123")
    mock_session.delete.assert_not_called()


def test_postgres_remove_success(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing = MagicMock(spec=EphemeralSecret)
    mock_filter.first.return_value = existing
    store.remove("my_key", task_id="task_123")
    mock_session.delete.assert_called_once_with(existing)
    mock_session.flush.assert_called_once()


def test_postgres_delete_task_secrets(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    mock_filter.delete.return_value = 5

    store.delete_task_secrets("task_123")
    mock_session.flush.assert_called_once()


def test_postgres_refresh_task_ttl(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    mock_filter.update.return_value = 5

    store.refresh_task_ttl("task_123")
    mock_session.flush.assert_called_once()


def test_postgres_get_helper(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    val = store.get("my_key", task_id="task_123")
    assert val == "decrypted_value"


def test_postgres_has_helper(mock_session, mock_encrypted_json):
    store = PostgresEphemeralSecretStore(mock_session)
    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    assert store.has("my_key", task_id="task_123") is True


def test_session_factory_store_record(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    record = EphemeralSecretRecord(
        handle_id="test_key",
        task_id="test_task",
        value="test_value",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
    )

    store.store_record(record)
    mock_factory.assert_called_once()
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


def test_session_factory_store_helper(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    store.store("my_key", "my_val", task_id="task_123")
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


def test_session_factory_get_record(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    store.get_record("my_key", task_id="task_123")
    mock_session.close.assert_called_once()


def test_session_factory_get(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    store.get("my_key", task_id="task_123")
    mock_session.close.assert_called_once()


def test_session_factory_has(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    mock_query = mock_session.query.return_value
    mock_filter = mock_query.filter_by.return_value
    existing_secret = MagicMock(spec=EphemeralSecret)
    existing_secret.expires_at = datetime.now(UTC) + timedelta(hours=1)
    existing_secret.handle_id = "my_key"
    existing_secret.task_id = "task_123"
    existing_secret.encrypted_value = b"enc"
    existing_secret.metadata_payload = {
        "required_scope": "custom",
        "exposure_policy": "sandbox_env",
        "permitted_egress_hosts": [],
        "destination_env_var": None,
        "destination_mount_path": None,
    }
    mock_filter.first.return_value = existing_secret

    store.has("my_key", task_id="task_123")
    mock_session.close.assert_called_once()


def test_session_factory_remove(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    store.remove("my_key", task_id="task_123")
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


def test_session_factory_refresh_task_ttl(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    store.refresh_task_ttl("task_123")
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()


def test_session_factory_delete_task_secrets(mock_encrypted_json):
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)
    store = SessionFactoryEphemeralSecretStore(mock_factory)

    store.delete_task_secrets("task_123")
    mock_session.commit.assert_called_once()
    mock_session.close.assert_called_once()

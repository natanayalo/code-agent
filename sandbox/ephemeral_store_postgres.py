"""PostgreSQL-backed ephemeral secret store using SQLAlchemy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from db.models import EncryptedJSON, EphemeralSecret
from sandbox.secrets import (
    EphemeralSecretRecord,
    EphemeralSecretStore,
    SecretExposurePolicy,
    SecretScope,
)


class PostgresEphemeralSecretStore(EphemeralSecretStore):
    """Postgres-backed ephemeral secret store that enlists in the caller's session.

    This store delegates symmetric encryption to the active CODE_AGENT_ENCRYPTION_KEY
    via the Fernet configuration in EncryptedJSON.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._encrypted_json = EncryptedJSON()

    def store_record(self, record: EphemeralSecretRecord, *, ttl_seconds: int = 3600) -> str:
        if not self._encrypted_json.is_active():
            raise RuntimeError("PostgresEphemeralSecretStore requires an active encryption key.")

        fernet = self._encrypted_json.fernet
        if fernet is None:
            raise RuntimeError("Fernet key not available")

        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        encrypted_bytes = fernet.encrypt(record.value.encode("utf-8"))

        metadata = {
            "required_scope": record.required_scope.value,
            "exposure_policy": record.exposure_policy.value,
            "permitted_egress_hosts": record.permitted_egress_hosts,
            "destination_env_var": record.destination_env_var,
            "destination_mount_path": record.destination_mount_path,
        }

        existing = (
            self._session.query(EphemeralSecret)
            .filter_by(task_id=record.task_id, handle_id=record.handle_id)
            .first()
        )
        if existing:
            existing.encrypted_value = encrypted_bytes
            existing.metadata_payload = metadata
            existing.expires_at = expires_at
        else:
            secret = EphemeralSecret(
                task_id=record.task_id,
                handle_id=record.handle_id,
                encrypted_value=encrypted_bytes,
                metadata_payload=metadata,
                expires_at=expires_at,
            )
            self._session.add(secret)

        self._session.flush()
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

        record = EphemeralSecretRecord(
            handle_id=key,
            task_id=task_id,
            value=value,
            required_scope=scope,
            exposure_policy=exposure_policy,
        )
        return self.store_record(record, ttl_seconds=ttl_seconds)

    def get_record(
        self, handle_id: str, *, task_id: str | None = None
    ) -> EphemeralSecretRecord | None:
        if not task_id:
            return None

        if not self._encrypted_json.is_active():
            raise RuntimeError("PostgresEphemeralSecretStore requires an active encryption key.")

        fernet = self._encrypted_json.fernet
        if fernet is None:
            raise RuntimeError("Fernet key not available")

        secret = (
            self._session.query(EphemeralSecret)
            .filter_by(task_id=task_id, handle_id=handle_id)
            .first()
        )

        if not secret:
            return None

        if secret.expires_at:
            expires_at = secret.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                self._session.delete(secret)
                self._session.flush()
                return None

        try:
            plaintext = fernet.decrypt(secret.encrypted_value).decode("utf-8")
        except Exception as e:
            raise RuntimeError("Failed to decrypt ephemeral secret") from e

        meta = secret.metadata_payload

        return EphemeralSecretRecord(
            handle_id=secret.handle_id,
            task_id=secret.task_id,
            value=plaintext,
            required_scope=SecretScope(meta["required_scope"]),
            exposure_policy=SecretExposurePolicy(meta["exposure_policy"]),
            permitted_egress_hosts=tuple(meta.get("permitted_egress_hosts", [])),
            destination_env_var=meta.get("destination_env_var"),
            destination_mount_path=meta.get("destination_mount_path"),
        )

    def get(self, handle_or_key: str, *, task_id: str | None = None) -> str | None:
        rec = self.get_record(handle_or_key, task_id=task_id)
        return rec.value if rec is not None else None

    def has(self, handle_or_key: str, *, task_id: str | None = None) -> bool:
        return self.get_record(handle_or_key, task_id=task_id) is not None

    def remove(self, handle_or_key: str, *, task_id: str | None = None) -> None:
        if not task_id:
            return

        secret = (
            self._session.query(EphemeralSecret)
            .filter_by(task_id=task_id, handle_id=handle_or_key)
            .first()
        )
        if secret:
            self._session.delete(secret)
            self._session.flush()

    def refresh_task_ttl(self, task_id: str, *, ttl_seconds: int = 3600) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        self._session.query(EphemeralSecret).filter_by(task_id=task_id).update(
            {"expires_at": expires_at}
        )
        self._session.flush()

    def delete_task_secrets(self, task_id: str) -> None:
        self._session.query(EphemeralSecret).filter_by(task_id=task_id).delete()
        self._session.flush()


class SessionFactoryEphemeralSecretStore(EphemeralSecretStore):
    """Postgres-backed ephemeral secret store that manages its own sessions.

    This store takes a session factory and wraps all operations in their own
    transactions, making it safe to inject into long-lived application components
    like workers that process many tasks.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def store_record(self, record: EphemeralSecretRecord, *, ttl_seconds: int = 3600) -> str:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            return PostgresEphemeralSecretStore(session).store_record(
                record, ttl_seconds=ttl_seconds
            )

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
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            return PostgresEphemeralSecretStore(session).store(
                key,
                value,
                task_id=task_id,
                scope=scope,
                exposure_policy=exposure_policy,
                ttl_seconds=ttl_seconds,
            )

    def get_record(
        self, handle_id: str, *, task_id: str | None = None
    ) -> EphemeralSecretRecord | None:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            return PostgresEphemeralSecretStore(session).get_record(handle_id, task_id=task_id)

    def get(self, handle_or_key: str, *, task_id: str | None = None) -> str | None:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            return PostgresEphemeralSecretStore(session).get(handle_or_key, task_id=task_id)

    def has(self, handle_or_key: str, *, task_id: str | None = None) -> bool:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            return PostgresEphemeralSecretStore(session).has(handle_or_key, task_id=task_id)

    def remove(self, handle_or_key: str, *, task_id: str | None = None) -> None:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            PostgresEphemeralSecretStore(session).remove(handle_or_key, task_id=task_id)

    def refresh_task_ttl(self, task_id: str, *, ttl_seconds: int = 3600) -> None:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            PostgresEphemeralSecretStore(session).refresh_task_ttl(task_id, ttl_seconds=ttl_seconds)

    def delete_task_secrets(self, task_id: str) -> None:
        from repositories import session_scope

        with session_scope(self._session_factory) as session:
            PostgresEphemeralSecretStore(session).delete_task_secrets(task_id)

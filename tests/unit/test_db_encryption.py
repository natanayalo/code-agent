"""Unit tests for database-level secret encryption."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from _pytest.monkeypatch import MonkeyPatch
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

from db.models import EncryptedJSON

Base = declarative_base()


class _TestModel(Base):
    __tablename__ = "test_encrypted"
    id = Column(Integer, primary_key=True)
    data = Column(EncryptedJSON)


def test_encrypted_json_roundtrip_with_key() -> None:
    """EncryptedJSON should correctly encrypt on save and decrypt on load when a key is present."""
    key = Fernet.generate_key().decode()
    with patch.dict(os.environ, {"CODE_AGENT_ENCRYPTION_KEY": key}):
        # We need to re-initialize the TypeDecorator to pick up the new env var
        decorator = EncryptedJSON()
        assert decorator.fernet is not None

        original_data = {"foo": "bar", "secret": "psst"}

        # Test encryption (process_bind_param)
        encrypted_value = decorator.process_bind_param(original_data, None)
        assert isinstance(encrypted_value, str)
        assert encrypted_value != json.dumps(original_data)

        # Verify it's valid Fernet
        fernet = Fernet(key.encode())
        decrypted_json = fernet.decrypt(encrypted_value.encode()).decode()
        assert json.loads(decrypted_json) == original_data

        # Test decryption (process_result_value)
        decrypted_data = decorator.process_result_value(encrypted_value, None)
        assert decrypted_data == original_data


def test_encrypted_json_fallback_without_key() -> None:
    """EncryptedJSON should fall back to plain JSON if no encryption key is configured."""
    with patch.dict(os.environ, {}, clear=True):
        decorator = EncryptedJSON()
        assert decorator.fernet is None

        original_data = {"foo": "bar"}

        # Should be plain JSON string
        encrypted_value = decorator.process_bind_param(original_data, None)
        assert encrypted_value == json.dumps(original_data)

        # Should be correctly loaded back
        decrypted_data = decorator.process_result_value(encrypted_value, None)
        assert decrypted_data == original_data


def test_encrypted_json_handles_decryption_failure_gracefully() -> None:
    """Failed decryption (e.g. wrong key) should return the raw value with a warning."""
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    # Encrypt with Key A
    with patch.dict(os.environ, {"CODE_AGENT_ENCRYPTION_KEY": key_a}):
        decorator_a = EncryptedJSON()
        encrypted_val = decorator_a.process_bind_param({"secret": "data"}, None)

    # Try to decrypt with Key B
    with patch.dict(os.environ, {"CODE_AGENT_ENCRYPTION_KEY": key_b}):
        decorator_b = EncryptedJSON()
        # This will fail decryption, and then fail json.loads, returning empty dict
        result = decorator_b.process_result_value(encrypted_val, None)
        assert result == {}


def test_encrypted_json_fails_fast_on_invalid_key(monkeypatch: MonkeyPatch) -> None:
    """If a key is provided but invalid, instantiation must fail loudly."""
    monkeypatch.setenv("CODE_AGENT_ENCRYPTION_KEY", "invalid-key-not-base64-or-wrong-length")

    with pytest.raises(RuntimeError, match="Encryption is configured but the key is invalid"):
        EncryptedJSON()

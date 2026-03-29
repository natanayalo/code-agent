"""Shared SQLAlchemy declarative base and column mixins."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from sqlalchemy import DateTime, MetaData, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Return the current UTC timestamp."""
    return datetime.now(UTC)


def generate_uuid() -> str:
    """Generate a string UUID for primary keys."""
    return str(uuid4())


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """Provide a string UUID primary key."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)


class TimestampMixin:
    """Provide created/updated timestamps in UTC."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
    )

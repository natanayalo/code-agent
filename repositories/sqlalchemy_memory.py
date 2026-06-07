"""Skeptical-memory SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.models import PersonalMemory, ProjectMemory
from repositories.sqlalchemy_common import UNSET, apply_memory_metadata


class PersonalMemoryRepository:
    """Persist and query personal memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, user_id: str, memory_key: str) -> PersonalMemory | None:
        statement = select(PersonalMemory).where(
            PersonalMemory.user_id == user_id,
            PersonalMemory.memory_key == memory_key,
        )
        return self.session.scalar(statement)

    def list_by_user(self, user_id: str) -> list[PersonalMemory]:
        statement = (
            select(PersonalMemory)
            .where(PersonalMemory.user_id == user_id)
            .order_by(PersonalMemory.created_at.desc(), PersonalMemory.id.desc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PersonalMemory]:
        statement = select(PersonalMemory)
        if user_id is not None:
            statement = statement.where(PersonalMemory.user_id == user_id)
        statement = (
            statement.order_by(PersonalMemory.created_at.desc(), PersonalMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        user_id: str,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = UNSET,
        confidence: float | object = UNSET,
        scope: str | None | object = UNSET,
        last_verified_at: datetime | None | object = UNSET,
        requires_verification: bool | object = UNSET,
    ) -> PersonalMemory:
        memory_entry = self.get(user_id=user_id, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = PersonalMemory(
                user_id=user_id,
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(user_id=user_id, memory_key=memory_key)
                if memory_entry is None:
                    raise
        apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, user_id: str, memory_key: str) -> bool:
        memory_entry = self.get(user_id=user_id, memory_key=memory_key)
        if memory_entry is None:
            return False
        self.session.delete(memory_entry)
        self.session.flush()
        return True


class ProjectMemoryRepository:
    """Persist and query project memory entries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, repo_url: str, memory_key: str) -> ProjectMemory | None:
        statement = select(ProjectMemory).where(
            ProjectMemory.repo_url == repo_url,
            ProjectMemory.memory_key == memory_key,
        )
        return self.session.scalar(statement)

    def list_by_repo(self, repo_url: str) -> list[ProjectMemory]:
        statement = (
            select(ProjectMemory)
            .where(ProjectMemory.repo_url == repo_url)
            .order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        repo_url: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProjectMemory]:
        statement = select(ProjectMemory)
        if repo_url is not None:
            statement = statement.where(ProjectMemory.repo_url == repo_url)
        statement = (
            statement.order_by(ProjectMemory.created_at.desc(), ProjectMemory.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        repo_url: str,
        memory_key: str,
        value: dict[str, Any],
        source: str | None | object = UNSET,
        confidence: float | object = UNSET,
        scope: str | None | object = UNSET,
        last_verified_at: datetime | None | object = UNSET,
        requires_verification: bool | object = UNSET,
    ) -> ProjectMemory:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = ProjectMemory(
                repo_url=repo_url,
                memory_key=memory_key,
                value=value,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(memory_entry)
                    self.session.flush()
            except IntegrityError:
                memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
                if memory_entry is None:
                    raise
        apply_memory_metadata(
            memory_entry,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            last_verified_at=last_verified_at,
            requires_verification=requires_verification,
        )
        self.session.flush()
        return memory_entry

    def delete(self, *, repo_url: str, memory_key: str) -> bool:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            return False
        self.session.delete(memory_entry)
        self.session.flush()
        return True

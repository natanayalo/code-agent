"""Session- and user-oriented SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from db.models import Session as ConversationSession
from db.models import SessionState, User


class UserRepository:
    """Persist and query users."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        external_user_id: str | None = None,
        display_name: str | None = None,
    ) -> User:
        user = User(external_user_id=external_user_id, display_name=display_name)
        self.session.add(user)
        self.session.flush()
        return user

    def get(self, user_id: str) -> User | None:
        return self.session.get(User, user_id)

    def get_by_external_user_id(self, external_user_id: str) -> User | None:
        statement = select(User).where(User.external_user_id == external_user_id)
        return self.session.scalar(statement)


class SessionRepository:
    """Persist and query conversation sessions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        user_id: str,
        channel: str,
        external_thread_id: str,
        active_task_id: str | None = None,
        status: str = "active",
        last_seen_at: datetime | None = None,
    ) -> ConversationSession:
        conversation_session = ConversationSession(
            user_id=user_id,
            channel=channel,
            external_thread_id=external_thread_id,
            active_task_id=active_task_id,
            status=status,
            last_seen_at=last_seen_at,
        )
        self.session.add(conversation_session)
        self.session.flush()
        return conversation_session

    def get(self, session_id: str) -> ConversationSession | None:
        statement = (
            select(ConversationSession)
            .options(selectinload(ConversationSession.session_state))
            .where(ConversationSession.id == session_id)
        )
        return self.session.scalar(statement)

    def get_by_channel_thread(
        self,
        *,
        channel: str,
        external_thread_id: str,
    ) -> ConversationSession | None:
        statement = select(ConversationSession).where(
            ConversationSession.channel == channel,
            ConversationSession.external_thread_id == external_thread_id,
        )
        return self.session.scalar(statement)

    def list_by_user(self, user_id: str) -> list[ConversationSession]:
        statement = (
            select(ConversationSession)
            .where(ConversationSession.user_id == user_id)
            .order_by(ConversationSession.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def list_all(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSession]:
        """List all sessions with pagination."""
        statement = (
            select(ConversationSession)
            .options(selectinload(ConversationSession.session_state))
            .order_by(ConversationSession.created_at.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        return list(self.session.scalars(statement))

    def set_active_task(
        self,
        *,
        session_id: str,
        active_task_id: str | None,
    ) -> ConversationSession | None:
        conversation_session = self.get(session_id)
        if conversation_session is None:
            return None

        conversation_session.active_task_id = active_task_id
        self.session.flush()
        return conversation_session

    def touch(
        self,
        *,
        session_id: str,
        seen_at: datetime,
    ) -> ConversationSession | None:
        conversation_session = self.get(session_id)
        if conversation_session is None:
            return None

        conversation_session.last_seen_at = seen_at
        self.session.flush()
        return conversation_session


class SessionStateRepository:
    """Persist and query compact session working state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, session_id: str) -> SessionState | None:
        statement = select(SessionState).where(SessionState.session_id == session_id)
        return self.session.scalar(statement)

    def upsert(
        self,
        *,
        session_id: str,
        active_goal: str | None = None,
        decisions_made: dict[str, Any] | None = None,
        identified_risks: dict[str, Any] | None = None,
        files_touched: list[str] | None = None,
    ) -> SessionState:
        state = self.get(session_id)
        if state is None:
            state = SessionState(
                session_id=session_id,
                active_goal=active_goal,
                decisions_made=decisions_made or {},
                identified_risks=identified_risks or {},
                files_touched=files_touched or [],
            )
            try:
                with self.session.begin_nested():
                    self.session.add(state)
                    self.session.flush()
                return state
            except IntegrityError:
                state = self.get(session_id)
                if state is None:
                    raise

        if active_goal is not None:
            state.active_goal = active_goal
        if decisions_made is not None:
            state.decisions_made = {**(state.decisions_made or {}), **decisions_made}
        if identified_risks is not None:
            state.identified_risks = {**(state.identified_risks or {}), **identified_risks}
        if files_touched is not None:
            state.files_touched = list(
                dict.fromkeys([*(state.files_touched or []), *files_touched])
            )
        self.session.flush()
        return state

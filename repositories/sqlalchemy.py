"""SQLAlchemy-backed repositories for persistence entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (
    Artifact,
    PersonalMemory,
    ProjectMemory,
    Task,
    User,
    WorkerRun,
)
from db.models import (
    Session as ConversationSession,
)


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
        return self.session.get(ConversationSession, session_id)

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


class TaskRepository:
    """Persist and query tasks."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        session_id: str,
        task_text: str,
        repo_url: str | None = None,
        branch: str | None = None,
        status: str = "pending",
        priority: int = 0,
        chosen_worker: str | None = None,
        route_reason: str | None = None,
    ) -> Task:
        task = Task(
            session_id=session_id,
            task_text=task_text,
            repo_url=repo_url,
            branch=branch,
            status=status,
            priority=priority,
            chosen_worker=chosen_worker,
            route_reason=route_reason,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def get(self, task_id: str) -> Task | None:
        return self.session.get(Task, task_id)

    def list_by_session(self, session_id: str) -> list[Task]:
        statement = (
            select(Task).where(Task.session_id == session_id).order_by(Task.created_at.asc())
        )
        return list(self.session.scalars(statement))

    def set_route(
        self,
        *,
        task_id: str,
        chosen_worker: str,
        route_reason: str,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None

        task.chosen_worker = chosen_worker
        task.route_reason = route_reason
        self.session.flush()
        return task

    def update_status(self, *, task_id: str, status: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None

        task.status = status
        self.session.flush()
        return task


class WorkerRunRepository:
    """Persist and query worker runs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        worker_type: str,
        started_at: datetime,
        status: str,
        workspace_id: str | None = None,
        finished_at: datetime | None = None,
        summary: str | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int = 0,
        artifact_index: list[dict[str, Any]] | None = None,
    ) -> WorkerRun:
        worker_run = WorkerRun(
            task_id=task_id,
            worker_type=worker_type,
            workspace_id=workspace_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            summary=summary,
            commands_run=commands_run,
            files_changed_count=files_changed_count,
            artifact_index=artifact_index,
        )
        self.session.add(worker_run)
        self.session.flush()
        return worker_run

    def get(self, run_id: str) -> WorkerRun | None:
        return self.session.get(WorkerRun, run_id)

    def list_by_task(self, task_id: str) -> list[WorkerRun]:
        statement = (
            select(WorkerRun)
            .where(WorkerRun.task_id == task_id)
            .order_by(WorkerRun.started_at.asc())
        )
        return list(self.session.scalars(statement))

    def complete(
        self,
        *,
        run_id: str,
        status: str,
        finished_at: datetime,
        summary: str | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int | None = None,
        artifact_index: list[dict[str, Any]] | None = None,
    ) -> WorkerRun | None:
        worker_run = self.get(run_id)
        if worker_run is None:
            return None

        worker_run.status = status
        worker_run.finished_at = finished_at
        worker_run.summary = summary
        worker_run.commands_run = commands_run
        if files_changed_count is not None:
            worker_run.files_changed_count = files_changed_count
        worker_run.artifact_index = artifact_index
        self.session.flush()
        return worker_run


class ArtifactRepository:
    """Persist and query run artifacts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        run_id: str,
        artifact_type: str,
        name: str,
        uri: str,
        artifact_metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        artifact = Artifact(
            run_id=run_id,
            artifact_type=artifact_type,
            name=name,
            uri=uri,
            artifact_metadata=artifact_metadata,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def list_by_run(self, run_id: str) -> list[Artifact]:
        statement = (
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at.asc())
        )
        return list(self.session.scalars(statement))


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
        statement = select(PersonalMemory).where(PersonalMemory.user_id == user_id)
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        user_id: str,
        memory_key: str,
        value: dict[str, Any],
    ) -> PersonalMemory:
        memory_entry = self.get(user_id=user_id, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = PersonalMemory(
                user_id=user_id,
                memory_key=memory_key,
                value=value,
            )
            self.session.add(memory_entry)
        else:
            memory_entry.value = value

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
        statement = select(ProjectMemory).where(ProjectMemory.repo_url == repo_url)
        return list(self.session.scalars(statement))

    def upsert(
        self,
        *,
        repo_url: str,
        memory_key: str,
        value: dict[str, Any],
    ) -> ProjectMemory:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            memory_entry = ProjectMemory(
                repo_url=repo_url,
                memory_key=memory_key,
                value=value,
            )
            self.session.add(memory_entry)
        else:
            memory_entry.value = value

        self.session.flush()
        return memory_entry

    def delete(self, *, repo_url: str, memory_key: str) -> bool:
        memory_entry = self.get(repo_url=repo_url, memory_key=memory_key)
        if memory_entry is None:
            return False

        self.session.delete(memory_entry)
        self.session.flush()
        return True

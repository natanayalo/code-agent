"""Worker-run and artifact SQLAlchemy repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from db.enums import (
    ArtifactType,
    OrchestrationRuntime,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import Artifact, Task, WorkerRun


class WorkerRunRepository:
    """Persist and query worker runs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
        worker_type: str | WorkerType,
        started_at: datetime,
        status: str | WorkerRunStatus,
        workspace_id: str | None = None,
        finished_at: datetime | None = None,
        summary: str | None = None,
        requested_permission: str | None = None,
        budget_usage: dict[str, Any] | None = None,
        verifier_outcome: dict[str, Any] | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int = 0,
        files_changed: list[str] | None = None,
        artifact_index: list[dict[str, Any]] | None = None,
        runtime_manifest: dict[str, Any] | None = None,
        delivery_metadata: dict[str, Any] | None = None,
        retention_expires_at: datetime | None = None,
        worker_profile: str | None = None,
        runtime_mode: str | WorkerRuntimeMode | None = None,
        orchestration_runtime: str | OrchestrationRuntime | None = None,
    ) -> WorkerRun:
        worker_run = WorkerRun(
            task_id=task_id,
            session_id=session_id,
            worker_type=worker_type,
            workspace_id=workspace_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            worker_profile=worker_profile,
            runtime_mode=cast(WorkerRuntimeMode | None, runtime_mode),
            orchestration_runtime=cast(OrchestrationRuntime | None, orchestration_runtime),
            summary=summary,
            requested_permission=requested_permission,
            budget_usage=budget_usage,
            verifier_outcome=verifier_outcome,
            commands_run=commands_run,
            files_changed_count=files_changed_count,
            files_changed=files_changed,
            artifact_index=artifact_index,
            runtime_manifest=runtime_manifest,
            delivery_metadata=delivery_metadata,
            retention_expires_at=retention_expires_at,
        )
        self.session.add(worker_run)
        self.session.flush()
        return worker_run

    def create_for_task(
        self,
        *,
        task: Task,
        worker_type: str | WorkerType,
        started_at: datetime,
        status: str | WorkerRunStatus,
        **kwargs: Any,
    ) -> WorkerRun:
        """Create run evidence using the runtime pinned on its parent task."""

        return self.create(
            task_id=task.id,
            session_id=task.session_id,
            worker_type=worker_type,
            started_at=started_at,
            status=status,
            orchestration_runtime=task.orchestration_runtime,
            **kwargs,
        )

    def get(self, run_id: str) -> WorkerRun | None:
        return self.session.get(WorkerRun, run_id)

    def list_by_task(self, task_id: str) -> list[WorkerRun]:
        statement = (
            select(WorkerRun)
            .where(WorkerRun.task_id == task_id)
            .order_by(WorkerRun.started_at.asc(), WorkerRun.id.asc())
        )
        return list(self.session.scalars(statement))

    def list_retained_before(self, retention_expires_before: datetime) -> list[WorkerRun]:
        statement = (
            select(WorkerRun)
            .where(
                WorkerRun.retention_expires_at.is_not(None),
                WorkerRun.retention_expires_at <= retention_expires_before,
            )
            .order_by(WorkerRun.retention_expires_at.asc(), WorkerRun.started_at.asc())
        )
        return list(self.session.scalars(statement))

    def clear_artifact_index(self, run_id: str) -> WorkerRun | None:
        worker_run = self.get(run_id)
        if worker_run is None:
            return None
        worker_run.artifact_index = []
        self.session.flush()
        return worker_run

    def complete(
        self,
        *,
        run_id: str,
        status: str | WorkerRunStatus,
        finished_at: datetime,
        summary: str | None = None,
        requested_permission: str | None = None,
        budget_usage: dict[str, Any] | None = None,
        verifier_outcome: dict[str, Any] | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        files_changed_count: int | None = None,
        files_changed: list[str] | None = None,
        artifact_index: list[dict[str, Any]] | None = None,
    ) -> WorkerRun | None:
        worker_run = self.get(run_id)
        if worker_run is None:
            return None

        worker_run.status = cast(WorkerRunStatus, status)
        worker_run.finished_at = finished_at
        if summary is not None:
            worker_run.summary = summary
        if requested_permission is not None:
            worker_run.requested_permission = requested_permission
        if budget_usage is not None:
            worker_run.budget_usage = budget_usage
        if verifier_outcome is not None:
            worker_run.verifier_outcome = verifier_outcome
        if commands_run is not None:
            worker_run.commands_run = commands_run
        if files_changed_count is not None:
            worker_run.files_changed_count = files_changed_count
        if files_changed is not None:
            worker_run.files_changed = files_changed
        if artifact_index is not None:
            worker_run.artifact_index = artifact_index
        self.session.flush()
        return worker_run

    def get_metrics(self, since: datetime | None = None) -> dict[str, Any]:
        usage_stmt = select(WorkerRun.worker_type, func.count(WorkerRun.id)).group_by(
            WorkerRun.worker_type
        )
        if since:
            usage_stmt = usage_stmt.where(WorkerRun.started_at >= since)
        worker_usage = self.session.execute(usage_stmt).all()

        runtime_usage_stmt = select(WorkerRun.runtime_mode, func.count(WorkerRun.id)).group_by(
            WorkerRun.runtime_mode
        )
        if since:
            runtime_usage_stmt = runtime_usage_stmt.where(WorkerRun.started_at >= since)
        runtime_mode_usage = self.session.execute(runtime_usage_stmt).all()

        legacy_tool_loop_stmt = (
            select(WorkerRun.worker_type, func.count(WorkerRun.id))
            .where(
                WorkerRun.runtime_mode == WorkerRuntimeMode.TOOL_LOOP,
                WorkerRun.worker_type.in_((WorkerType.CODEX, WorkerType.ANTIGRAVITY)),
            )
            .group_by(WorkerRun.worker_type)
        )
        if since:
            legacy_tool_loop_stmt = legacy_tool_loop_stmt.where(WorkerRun.started_at >= since)
        legacy_tool_loop_usage = self.session.execute(legacy_tool_loop_stmt).all()

        duration_stmt = select(
            func.avg(
                func.extract("epoch", WorkerRun.finished_at)
                - func.extract("epoch", WorkerRun.started_at)
            ).label("avg_duration"),
            func.coalesce(
                func.sum(case((WorkerRun.status == WorkerRunStatus.SUCCESS, 1), else_=0)), 0
            ).label("success_count"),
            func.count(WorkerRun.id).label("total_count"),
        ).where(WorkerRun.finished_at.is_not(None))
        if since:
            duration_stmt = duration_stmt.where(WorkerRun.started_at >= since)
        duration_stats = self.session.execute(duration_stmt).one()

        return {
            "worker_usage": {
                (w.value if hasattr(w, "value") else str(w)): count for w, count in worker_usage
            },
            "runtime_mode_usage": {
                (m.value if hasattr(m, "value") else ("unknown" if m is None else str(m))): count
                for m, count in runtime_mode_usage
            },
            "legacy_tool_loop_usage": {
                (w.value if hasattr(w, "value") else str(w)): count
                for w, count in legacy_tool_loop_usage
            },
            "avg_duration_seconds": float(duration_stats.avg_duration or 0),
            "success_rate": (duration_stats.success_count / duration_stats.total_count)
            if duration_stats.total_count > 0
            else 0,
        }


class ArtifactRepository:
    """Persist and query run artifacts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        run_id: str,
        artifact_type: str | ArtifactType,
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

    def delete_by_run(self, run_id: str) -> int:
        statement = delete(Artifact).where(Artifact.run_id == run_id)
        result = self.session.execute(statement)
        self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

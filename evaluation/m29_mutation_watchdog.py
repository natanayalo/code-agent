"""72-hour workspace retention watchdog and durable ledger for M29 evaluation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evaluation.m29_mutation_isolation import (
    MAX_EVALUATION_RETENTION_SECONDS,
    build_evaluation_cleanup_policy,
    cleanup_evaluation_workspace,
    validate_evaluation_retention_seconds,
)
from sandbox.workspace import WorkspaceHandle, WorkspaceManager

LOGGER = logging.getLogger("m29_mutation_watchdog")


class RecordedEvaluationWorkspace(BaseModel):
    """A tracked evaluation workspace record in the durable retention ledger."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    workspace_path: str = Field(min_length=1)
    trusted_git_dir: str | None = None
    provisioned_at: datetime
    retention_deadline_at: datetime
    cleaned_up: bool = False
    cleaned_at: datetime | None = None
    cleanup_error: str | None = None


@dataclass(frozen=True)
class WatchdogSweepResult:
    """Summary of a retention watchdog sweep run."""

    total_recorded: int
    expired_count: int
    cleaned_count: int
    failed_count: int
    cleaned_workspace_ids: tuple[str, ...]
    failed_workspace_ids: tuple[str, ...]
    errors: dict[str, str]


class EvaluationRetentionWatchdog:
    """Durable 72-hour retention watchdog sweeping expired evaluation workspaces."""

    def __init__(
        self,
        ledger_dir_or_path: Path,
        retention_seconds: int = MAX_EVALUATION_RETENTION_SECONDS,
    ) -> None:
        validate_evaluation_retention_seconds(retention_seconds)
        self.retention_seconds = retention_seconds

        target = ledger_dir_or_path.resolve()
        if target.is_dir() or target.suffix != ".json":
            self.ledger_path = target / "retention_ledger.json"
        else:
            self.ledger_path = target

        self._records: dict[str, RecordedEvaluationWorkspace] = {}
        if self.ledger_path.exists():
            self._load_ledger()

    def _load_ledger(self) -> None:
        try:
            content = self.ledger_path.read_text(encoding="utf-8")
            if not content.strip():
                return
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    record = RecordedEvaluationWorkspace.model_validate(item)
                    self._records[record.workspace_id] = record
        except Exception as exc:
            LOGGER.error("Failed to load retention ledger from %s: %s", self.ledger_path, exc)
            raise

    def _save_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        items = [rec.model_dump(mode="json") for rec in self._records.values()]
        payload = json.dumps(items, indent=2, sort_keys=True, ensure_ascii=False)
        temp_path = self.ledger_path.with_suffix(".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        temp_path.replace(self.ledger_path)

    @property
    def records(self) -> tuple[RecordedEvaluationWorkspace, ...]:
        """Return all recorded workspaces."""
        return tuple(self._records.values())

    @property
    def uncleaned_records(self) -> tuple[RecordedEvaluationWorkspace, ...]:
        """Return all workspaces currently not cleaned."""
        return tuple(r for r in self._records.values() if not r.cleaned_up)

    def record_workspace(
        self,
        case_id: str,
        task_id: str,
        handle: WorkspaceHandle,
        *,
        now: datetime | None = None,
    ) -> RecordedEvaluationWorkspace:
        """Record newly provisioned workspace with an explicit 72-hour retention deadline."""
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        deadline = now_dt + timedelta(seconds=self.retention_seconds)
        git_dir = (
            str(handle.trusted_git_dir.resolve()) if handle.trusted_git_dir is not None else None
        )
        record = RecordedEvaluationWorkspace(
            case_id=case_id,
            task_id=task_id,
            workspace_id=handle.workspace_id,
            workspace_path=str(handle.workspace_path.resolve()),
            trusted_git_dir=git_dir,
            provisioned_at=now_dt,
            retention_deadline_at=deadline,
            cleaned_up=False,
        )
        self._records[handle.workspace_id] = record
        self._save_ledger()
        return record

    def mark_cleaned(
        self,
        workspace_id: str,
        *,
        now: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Mark an evaluation workspace as cleaned (or record cleanup error)."""
        record = self._records.get(workspace_id)
        if record is None:
            return
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        self._records[workspace_id] = record.model_copy(
            update={
                "cleaned_up": error is None,
                "cleaned_at": now_dt,
                "cleanup_error": error,
            }
        )
        self._save_ledger()

    def sweep_expired(
        self,
        workspace_manager: WorkspaceManager,
        *,
        now: datetime | None = None,
        force_all: bool = False,
    ) -> WatchdogSweepResult:
        """Sweep expired evaluation workspaces via WorkspaceManager boundaries."""
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        cleaned_ids: list[str] = []
        failed_ids: list[str] = []
        errors: dict[str, str] = {}
        expired_count = 0

        for record in list(self._records.values()):
            if record.cleaned_up:
                continue
            is_expired = force_all or (now_dt >= record.retention_deadline_at.astimezone(UTC))
            if not is_expired:
                continue
            expired_count += 1

            handle = WorkspaceHandle(
                workspace_id=record.workspace_id,
                task_id=record.task_id,
                workspace_path=Path(record.workspace_path),
                repo_path=Path(record.workspace_path),
                repo_url="",
                cleanup_policy=build_evaluation_cleanup_policy(),
                trusted_git_dir=Path(record.trusted_git_dir) if record.trusted_git_dir else None,
            )
            try:
                cleanup_evaluation_workspace(
                    workspace_manager,
                    handle,
                    succeeded=True,
                    recorded_workspace_ids={record.workspace_id},
                    recorded_workspace_paths={record.workspace_path},
                )
                self.mark_cleaned(record.workspace_id, now=now_dt)
                cleaned_ids.append(record.workspace_id)
            except Exception as exc:
                LOGGER.error(
                    "Retention watchdog sweep failed for workspace %s: %s",
                    record.workspace_id,
                    exc,
                    exc_info=True,
                )
                err_msg = str(exc)
                self.mark_cleaned(record.workspace_id, now=now_dt, error=err_msg)
                failed_ids.append(record.workspace_id)
                errors[record.workspace_id] = err_msg

        return WatchdogSweepResult(
            total_recorded=len(self._records),
            expired_count=expired_count,
            cleaned_count=len(cleaned_ids),
            failed_count=len(failed_ids),
            cleaned_workspace_ids=tuple(cleaned_ids),
            failed_workspace_ids=tuple(failed_ids),
            errors=errors,
        )


__all__ = [
    "EvaluationRetentionWatchdog",
    "RecordedEvaluationWorkspace",
    "WatchdogSweepResult",
]

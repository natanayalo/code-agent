"""Concrete candidate dispatcher and memory isolation manager for disposable stacks."""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from db.models import (
    MemoryAdmissionDecision,
    MemoryObservation,
    MemoryProposal,
    PersonalMemory,
    ProjectMemory,
    Task,
    WorkerRun,
)
from evaluation.m29_mutation_isolation import (
    TerminalSnapshot,
    build_evaluation_cleanup_policy,
    create_terminal_snapshot,
)
from evaluation.m29_mutation_runner_models import (
    CandidateDispatcher,
    CandidateTaskExecutionResult,
)
from evaluation.m29_mutation_runner_preflight import default_mutation_constraints
from evaluation.m29_mutation_suite import M29MutationCase
from evaluation.m29_mutation_watchdog import EvaluationRetentionWatchdog
from evaluation.provider_reliability_identity import (
    _extract_run_model_execution,
    resolve_task_execution_identity,
)
from evaluation.provider_reliability_models import ExecutionIdentity
from orchestrator.execution_types import TaskSnapshot
from orchestrator.state import MemoryContext, MemoryEntry, ObservationContextEntry
from sandbox.workspace import WorkspaceHandle

LOGGER = logging.getLogger("m29_mutation_dispatcher")


def _extract_workspace_diff(workspace_path: Path) -> str:
    """Extract git diff against HEAD in the workspace directory."""
    if not workspace_path.exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception as exc:
        LOGGER.warning("Failed to extract git diff from %s: %s", workspace_path, exc)
    return ""


def _extract_changed_files(workspace_path: Path) -> tuple[str, ...]:
    """Extract changed files list from git status and diff."""
    if not workspace_path.exists():
        return ()
    files: set[str] = set()
    try:
        proc_diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_diff.returncode == 0:
            files.update(line.strip() for line in proc_diff.stdout.splitlines() if line.strip())

        proc_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_status.returncode == 0:
            for line in proc_status.stdout.splitlines():
                if len(line) >= 4:
                    status_code = line[:2]
                    file_path = line[3:].strip()
                    if status_code in ("??", "A ", " M", "M "):
                        files.add(file_path)
    except Exception as exc:
        LOGGER.warning("Failed to extract changed files from %s: %s", workspace_path, exc)
    return tuple(sorted(files))


class DisposableDbMemoryIsolationManager:
    """Manages frozen memory context loading and inter-case isolation in a disposable database."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.engine = create_engine(db_url, echo=False)
        self.session_factory = sessionmaker(bind=self.engine)

    def load_memory_context(self, repo_url: str) -> MemoryContext:
        """Load frozen MemoryContext asserting empty personal memory store."""
        with self.session_factory() as session:
            pers_rows = session.scalars(select(PersonalMemory)).all()
            if pers_rows:
                msg = f"Disposable database personal memory is not empty: {len(pers_rows)} entries"
                raise ValueError(msg)

            proj_rows = session.scalars(
                select(ProjectMemory).where(ProjectMemory.repo_url == repo_url)
            ).all()
            project_entries = [
                MemoryEntry(
                    memory_key=pm.memory_key,
                    value=pm.value,
                    source=pm.source,
                    confidence=pm.confidence,
                    scope=pm.scope,
                    last_verified_at=pm.last_verified_at,
                    requires_verification=pm.requires_verification,
                )
                for pm in proj_rows
            ]

            obs_rows = session.scalars(
                select(MemoryObservation).where(MemoryObservation.repo_url == repo_url)
            ).all()
            obs_entries = [
                ObservationContextEntry(
                    id=obs.id,
                    observed_at=obs.observed_at,
                    source=obs.source,
                    event_type=obs.event_type,
                    summary=obs.summary,
                    privacy_stripped=obs.privacy_stripped,
                )
                for obs in obs_rows
            ]

            return MemoryContext(
                personal=[],
                project=sorted(project_entries, key=lambda x: x.memory_key),
                session={},
                observations=sorted(obs_entries, key=lambda x: x.id),
            )

    def discard_case_memory_writes(self, repo_url: str | None = None) -> None:
        """Purge case-scoped episodic and proposed memories written during candidate execution."""
        with self.session_factory() as session:
            stmt_obs = delete(MemoryObservation)
            if repo_url:
                stmt_obs = stmt_obs.where(MemoryObservation.repo_url == repo_url)
            session.execute(stmt_obs)

            stmt_prop = delete(MemoryProposal)
            if repo_url:
                stmt_prop = stmt_prop.where(MemoryProposal.repo_url == repo_url)
            session.execute(stmt_prop)

            session.execute(delete(MemoryAdmissionDecision))
            session.execute(delete(PersonalMemory))
            session.commit()


class HttpStackCandidateDispatcher(CandidateDispatcher):
    """Candidate dispatcher sending requests to a disposable HTTP API stack."""

    def __init__(
        self,
        *,
        api_base_url: str,
        db_url: str,
        workspace_root: Path,
        repo_key: str = "taskboard",
        branch: str = "main",
        auth_token: str | None = None,
        poll_interval_seconds: float = 1.0,
        max_poll_seconds: float = 1200.0,
        retention_watchdog: EvaluationRetentionWatchdog | None = None,
        httpx_client: httpx.Client | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.db_url = db_url
        self.workspace_root = workspace_root.resolve()
        self.repo_key = repo_key
        self.branch = branch
        self.auth_token = auth_token
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_seconds = max_poll_seconds
        self.retention_watchdog = retention_watchdog
        self._external_client = httpx_client
        self._db_engine = create_engine(db_url, echo=False)
        self._session_factory = sessionmaker(bind=self._db_engine)

    def _get_client(self) -> httpx.Client:
        if self._external_client is not None:
            return self._external_client
        return httpx.Client(timeout=30.0)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["X-Webhook-Token"] = self.auth_token
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _submit_task(self, case: M29MutationCase) -> TaskSnapshot:
        worker_override = "codex" if "codex" in case.worker_profile else "antigravity"
        thread_id = f"m29-case-{case.case_id}-{uuid.uuid4().hex[:8]}"
        payload = {
            "task_text": case.prompt,
            "repo_key": self.repo_key,
            "branch": self.branch,
            "worker_override": worker_override,
            "worker_profile_override": case.worker_profile,
            "post_terminal_quality_evaluation": True,
            "constraints": default_mutation_constraints(),
            "budget": {"worker_timeout_seconds": case.candidate_timeout_seconds},
            "session": {
                "channel": "http",
                "external_user_id": "evaluation-m29",
                "external_thread_id": thread_id,
            },
        }

        client = self._get_client()
        resp = client.post(
            f"{self.api_base_url}/tasks",
            headers=self._get_headers(),
            json=payload,
        )
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"Task submission failed with HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return TaskSnapshot.model_validate(resp.json())

    def _poll_task(self, task_id: str) -> tuple[TaskSnapshot, bool]:
        client = self._get_client()
        headers = self._get_headers()
        start = time.time()
        latest_snap: TaskSnapshot | None = None

        while True:
            resp = client.get(f"{self.api_base_url}/tasks/{task_id}", headers=headers)
            if resp.status_code == 200:
                latest_snap = TaskSnapshot.model_validate(resp.json())
                if latest_snap.pending_interactions:
                    return latest_snap, True
                if latest_snap.status in {"completed", "failed", "cancelled"}:
                    return latest_snap, False

            if (time.time() - start) > self.max_poll_seconds:
                break
            time.sleep(self.poll_interval_seconds)

        if latest_snap is not None:
            return latest_snap, False
        raise TimeoutError(f"Polling task {task_id} exceeded {self.max_poll_seconds}s")

    def _resolve_workspace_handle(self, task_id: str, snapshot: TaskSnapshot) -> WorkspaceHandle:
        ws_id: str | None = None
        if snapshot.latest_run and snapshot.latest_run.workspace_id:
            ws_id = snapshot.latest_run.workspace_id
        elif snapshot.execution_plan:
            for node in reversed(snapshot.execution_plan.nodes):
                for attempt in reversed(node.attempts):
                    if attempt.workspace_id:
                        ws_id = attempt.workspace_id
                        break
                if ws_id:
                    break

        if not ws_id:
            ws_id = f"workspace-{task_id[:8]}"

        ws_path = (self.workspace_root / ws_id).resolve()
        trusted_git = (self.workspace_root / ".code-agent-git" / ws_id).resolve()
        return WorkspaceHandle(
            workspace_id=ws_id,
            task_id=task_id,
            workspace_path=ws_path,
            repo_path=ws_path,
            repo_url="",
            cleanup_policy=build_evaluation_cleanup_policy(),
            trusted_git_dir=trusted_git if trusted_git.exists() else None,
        )

    def _resolve_execution_identity(
        self, task_id: str, snapshot: TaskSnapshot
    ) -> tuple[ExecutionIdentity | None, str]:
        try:
            with self._session_factory() as session:
                task = session.get(Task, task_id)
                if task is not None:
                    runs = session.scalars(
                        select(WorkerRun)
                        .where(WorkerRun.task_id == task_id)
                        .order_by(WorkerRun.created_at)
                    ).all()
                    identity, status = resolve_task_execution_identity(task, list(runs))
                    return identity, status
        except Exception as exc:
            LOGGER.warning("Database execution identity query failed: %s", exc)

        if snapshot.latest_run and snapshot.latest_run.budget_usage:
            raw_meta, status = _extract_run_model_execution(
                WorkerRun(budget_usage=snapshot.latest_run.budget_usage)
            )
            if status == "verified" and raw_meta:
                return (
                    ExecutionIdentity(
                        provider=raw_meta.get("provider", ""),
                        model=raw_meta.get("model", ""),
                        reasoning_effort=raw_meta.get("reasoning_effort"),
                    ),
                    "verified",
                )
            if status == "mixed":
                return None, "mixed_execution_identity"

        return None, "unknown_legacy"

    def dispatch_case(
        self, case: M29MutationCase, memory_context: MemoryContext
    ) -> CandidateTaskExecutionResult:
        """Submit case via POST /tasks, poll until terminal, and extract outcome."""
        start_time = datetime.now(UTC)
        initial_snap = self._submit_task(case)
        task_id = initial_snap.task_id

        terminal_snap, pending_interaction = self._poll_task(task_id)
        finish_time = datetime.now(UTC)
        elapsed_sec = (finish_time - start_time).total_seconds()

        ws_handle = self._resolve_workspace_handle(task_id, terminal_snap)
        if self.retention_watchdog is not None:
            self.retention_watchdog.record_workspace(case.case_id, task_id, ws_handle)

        diff_text = _extract_workspace_diff(ws_handle.workspace_path)
        diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest() if diff_text else None
        changed_files = _extract_changed_files(ws_handle.workspace_path)

        exec_identity, identity_status = self._resolve_execution_identity(task_id, terminal_snap)

        terminal_status = terminal_snap.status
        terminal_event_type = (
            "interaction_requested" if pending_interaction else f"task_{terminal_status}"
        )
        failure_kind = terminal_snap.last_error
        if not failure_kind and terminal_snap.execution_plan:
            for node in reversed(terminal_snap.execution_plan.nodes):
                if node.failure_kind:
                    failure_kind = node.failure_kind
                    break
                for attempt in reversed(node.attempts):
                    if attempt.failure_kind:
                        failure_kind = attempt.failure_kind
                        break
                if failure_kind:
                    break

        return CandidateTaskExecutionResult(
            task_id=task_id,
            workspace_handle=ws_handle,
            task_status=terminal_status,
            terminal_event_type=terminal_event_type,
            terminal_timestamp=finish_time,
            failure_kind=failure_kind,
            diff_sha256=diff_sha,
            diff_text=diff_text,
            changed_files=changed_files,
            execution_identity=exec_identity,
            execution_identity_status=identity_status,
            time_to_terminal_seconds=elapsed_sec,
            pending_interaction=pending_interaction,
            task_spec=terminal_snap.task_spec,
            constraints=terminal_snap.constraints,
        )

    def capture_terminal_snapshot(
        self, case: M29MutationCase, cand_res: CandidateTaskExecutionResult
    ) -> TerminalSnapshot:
        """Re-query API task state and re-hash workspace diff to verify immutability."""
        task_status = cand_res.task_status
        terminal_event = cand_res.terminal_event_type
        failure_kind = cand_res.failure_kind
        ts = cand_res.terminal_timestamp

        try:
            client = self._get_client()
            resp = client.get(
                f"{self.api_base_url}/tasks/{cand_res.task_id}",
                headers=self._get_headers(),
            )
            if resp.status_code == 200:
                snap = TaskSnapshot.model_validate(resp.json())
                task_status = snap.status
                failure_kind = snap.last_error
                if not failure_kind and snap.execution_plan:
                    for node in reversed(snap.execution_plan.nodes):
                        if node.failure_kind:
                            failure_kind = node.failure_kind
                            break
                        for attempt in reversed(node.attempts):
                            if attempt.failure_kind:
                                failure_kind = attempt.failure_kind
                                break
                        if failure_kind:
                            break
        except Exception as exc:
            LOGGER.warning("Failed to re-fetch task state during snapshot capture: %s", exc)

        diff_text = _extract_workspace_diff(cand_res.workspace_handle.workspace_path)
        diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest() if diff_text else None
        changed_files = _extract_changed_files(cand_res.workspace_handle.workspace_path)

        return create_terminal_snapshot(
            task_status=task_status,
            terminal_event_type=terminal_event,
            terminal_timestamp=ts,
            failure_kind=failure_kind,
            diff_sha256=diff_sha,
            changed_files=changed_files,
        )


__all__ = [
    "DisposableDbMemoryIsolationManager",
    "HttpStackCandidateDispatcher",
]

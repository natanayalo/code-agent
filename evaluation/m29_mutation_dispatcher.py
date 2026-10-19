"""Concrete candidate dispatcher and memory isolation manager for disposable stacks."""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from db.models import (
    MemoryAdmissionDecision,
    MemoryObservation,
    MemoryProposal,
    PersonalMemory,
    ProjectMemory,
    SessionState,
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
from sandbox.audit import _should_ignore_path
from sandbox.workspace import WorkspaceHandle

LOGGER = logging.getLogger("m29_mutation_dispatcher")


def _build_git_cmd(workspace_path: Path, trusted_git_dir: Path | None = None) -> list[str]:
    cmd = ["git", "-c", "core.hooksPath=/dev/null"]
    if trusted_git_dir is not None and trusted_git_dir.exists():
        return [*cmd, f"--git-dir={trusted_git_dir}", f"--work-tree={workspace_path}"]
    return [*cmd, "-C", str(workspace_path)]


def _extract_workspace_diff(
    workspace_path: Path,
    target_revision: str | None = None,
    trusted_git_dir: Path | None = None,
) -> str:
    """Extract git diff against pinned baseline revision using trusted git directory if present."""
    if not workspace_path.exists():
        return ""
    try:
        git_cmd = _build_git_cmd(workspace_path, trusted_git_dir)
        target = target_revision or "HEAD"
        proc = subprocess.run(
            [*git_cmd, "diff", target],
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


def _extract_changed_files(
    workspace_path: Path,
    target_revision: str | None = None,
    trusted_git_dir: Path | None = None,
) -> tuple[str, ...]:
    """Extract changed files list from git status and diff against baseline revision."""
    if not workspace_path.exists():
        return ()
    files: set[str] = set()
    git_cmd = _build_git_cmd(workspace_path, trusted_git_dir)
    target = target_revision or "HEAD"
    try:
        proc_diff = subprocess.run(
            [*git_cmd, "diff", "--name-only", target],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_diff.returncode == 0:
            for line in proc_diff.stdout.splitlines():
                path = line.strip().rstrip("/")
                if path and not _should_ignore_path(path):
                    files.add(path)

        proc_status = subprocess.run(
            [*git_cmd, "status", "--porcelain", "--untracked-files=all"],
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
                    if " -> " in file_path:
                        file_path = file_path.split(" -> ")[-1].strip()
                    if status_code in ("??", "A ", " M", "M ", "AM", "D ", " D", "R "):
                        clean_path = file_path.rstrip("/")
                        if clean_path and not _should_ignore_path(clean_path):
                            files.add(clean_path)
    except Exception as exc:
        LOGGER.warning("Failed to extract changed files from %s: %s", workspace_path, exc)
    return tuple(sorted(files))


class DisposableDbMemoryIsolationManager:
    """Manages frozen memory context loading and inter-case isolation in a disposable database."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.engine = create_engine(db_url, echo=False)
        self.session_factory = sessionmaker(bind=self.engine)
        self._baseline_project_memory: dict[tuple[str, str], dict[str, Any]] | None = None

    def _capture_baseline_project_memory(self, session: Session) -> None:
        if self._baseline_project_memory is None:
            all_proj = session.scalars(select(ProjectMemory)).all()
            self._baseline_project_memory = {
                (pm.repo_url, pm.memory_key): {
                    "value": pm.value,
                    "source": pm.source,
                    "confidence": pm.confidence,
                    "scope": pm.scope,
                    "last_verified_at": pm.last_verified_at,
                    "requires_verification": pm.requires_verification,
                }
                for pm in all_proj
            }

    def load_memory_context(self, repo_url: str | None = None) -> MemoryContext:
        """Load frozen MemoryContext asserting empty personal memory store."""
        with self.session_factory() as session:
            pers_rows = session.scalars(select(PersonalMemory)).all()
            if pers_rows:
                msg = f"Disposable database personal memory is not empty: {len(pers_rows)} entries"
                raise ValueError(msg)

            stmt_proj = select(ProjectMemory)
            if repo_url:
                stmt_proj = stmt_proj.where(
                    (ProjectMemory.repo_url == repo_url)
                    | (ProjectMemory.repo_url.contains(repo_url))
                )
            proj_rows = session.scalars(stmt_proj).all()
            if not proj_rows and repo_url:
                proj_rows = session.scalars(select(ProjectMemory)).all()

            self._capture_baseline_project_memory(session)

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

            stmt_obs = select(MemoryObservation)
            if repo_url:
                stmt_obs = stmt_obs.where(
                    (MemoryObservation.repo_url == repo_url)
                    | (MemoryObservation.repo_url.contains(repo_url))
                )
            obs_rows = session.scalars(stmt_obs).all()
            if not obs_rows and repo_url:
                obs_rows = session.scalars(select(MemoryObservation)).all()

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
        """Purge case-scoped episodic/proposed memory and restore ProjectMemory/SessionState."""
        with self.session_factory() as session:
            session.execute(delete(MemoryObservation))
            session.execute(delete(MemoryProposal))
            session.execute(delete(MemoryAdmissionDecision))
            session.execute(delete(PersonalMemory))
            session.execute(delete(SessionState))

            if self._baseline_project_memory is not None:
                current_rows = session.scalars(select(ProjectMemory)).all()
                current_map = {(r.repo_url, r.memory_key): r for r in current_rows}
                for key, base_data in self._baseline_project_memory.items():
                    if key in current_map:
                        row = current_map[key]
                        for field, val in base_data.items():
                            setattr(row, field, val)
                    else:
                        session.add(
                            ProjectMemory(
                                repo_url=key[0],
                                memory_key=key[1],
                                **base_data,
                            )
                        )
                for key, row in current_map.items():
                    if key not in self._baseline_project_memory:
                        session.delete(row)
            session.commit()


def _extract_failure_kind(snap: TaskSnapshot) -> str | None:
    """Extract failure kind from task last_error or execution plan attempt nodes."""
    if snap.last_error:
        return snap.last_error
    if snap.execution_plan:
        for node in reversed(snap.execution_plan.nodes):
            if node.failure_kind:
                return node.failure_kind
            for attempt in reversed(node.attempts):
                if attempt.failure_kind:
                    return attempt.failure_kind
    return None


def _extract_candidate_evidence(terminal_snap: TaskSnapshot) -> dict[str, Any]:
    """Extract timeline events, artifacts, repairs, and verification outcomes for stop gates."""
    timeline_events = tuple(terminal_snap.timeline)

    artifacts: list[dict[str, Any]] = []
    if terminal_snap.latest_run is not None:
        for a in terminal_snap.latest_run.artifact_index or ():
            artifacts.append(dict(a))
        for a in terminal_snap.latest_run.artifacts or ():
            artifacts.append(a.model_dump(mode="json") if hasattr(a, "model_dump") else dict(a))
    if terminal_snap.execution_plan:
        for node in terminal_snap.execution_plan.nodes:
            for a in node.output_artifacts or ():
                artifacts.append(dict(a))

    in_task_repairs = sum(
        max(node.retry_count, max(0, len(node.attempts) - 1))
        for node in (terminal_snap.execution_plan.nodes if terminal_snap.execution_plan else ())
    ) + sum(1 for ev in timeline_events if "repair" in getattr(ev, "event_type", "").lower())

    verification_events: list[str] = [
        getattr(ev, "event_type", "")
        for ev in timeline_events
        if any(k in getattr(ev, "event_type", "").lower() for k in ("verification", "verifier"))
    ]
    if terminal_snap.latest_run and terminal_snap.latest_run.verifier_outcome:
        verification_events.append("verifier_outcome_recorded")
    if terminal_snap.execution_plan:
        for node in terminal_snap.execution_plan.nodes:
            if node.verification_outcome:
                verification_events.append("node_verification_outcome_recorded")

    review_artifacts: list[str] = []
    for art in artifacts:
        name = art.get("name") or art.get("path") or ""
        atype = art.get("artifact_type") or art.get("type") or ""
        if "review" in name.lower() or "review" in atype.lower():
            review_artifacts.append(name or atype or "review_artifact")

    return {
        "timeline_events": timeline_events,
        "artifacts": tuple(artifacts),
        "in_task_repairs": in_task_repairs,
        "verification_events": tuple(verification_events),
        "review_artifacts": tuple(review_artifacts),
    }


def _derive_terminal_event_and_ts(
    snap: TaskSnapshot, default_ts: datetime, pending_interaction: bool = False
) -> tuple[str, datetime]:
    """Derive terminal event type and timestamp from latest persisted timeline event."""
    if snap.timeline:
        latest_ev = max(snap.timeline, key=lambda ev: (ev.sequence_number, ev.created_at))
        return latest_ev.event_type, latest_ev.created_at
    fallback_event = "interaction_requested" if pending_interaction else f"task_{snap.status}"
    return fallback_event, default_ts


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
        memory_isolation_manager: DisposableDbMemoryIsolationManager | None = None,
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
        self.memory_isolation_manager = (
            memory_isolation_manager or DisposableDbMemoryIsolationManager(db_url)
        )

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
                ident = ExecutionIdentity(
                    provider=raw_meta.get("provider", ""),
                    model=raw_meta.get("model", ""),
                    reasoning_effort=raw_meta.get("reasoning_effort"),
                )
                return ident, "verified"
            if status == "mixed":
                return None, "mixed_execution_identity"

        return None, "unknown_legacy"

    def discard_case_memory_writes(self, repo_url: str | None = None) -> None:
        """Purge case-scoped episodic and proposed memories from disposable database."""
        self.memory_isolation_manager.discard_case_memory_writes(repo_url)

    def dispatch_case(
        self, case: M29MutationCase, memory_context: MemoryContext
    ) -> CandidateTaskExecutionResult:
        """Submit case via POST /tasks, poll until terminal, and extract outcome."""
        try:
            if self.memory_isolation_manager._baseline_project_memory is None:
                with self.memory_isolation_manager.session_factory() as session:
                    self.memory_isolation_manager._capture_baseline_project_memory(session)

            start_time = datetime.now(UTC)
            initial_snap = self._submit_task(case)
            task_id = initial_snap.task_id

            terminal_snap, pending_interaction = self._poll_task(task_id)
            finish_time = datetime.now(UTC)
            elapsed_sec = (finish_time - start_time).total_seconds()

            ws_handle = self._resolve_workspace_handle(task_id, terminal_snap)
            if self.retention_watchdog is not None:
                self.retention_watchdog.record_workspace(case.case_id, task_id, ws_handle)

            diff_text = _extract_workspace_diff(
                ws_handle.workspace_path,
                target_revision=case.target_repository_revision,
                trusted_git_dir=ws_handle.trusted_git_dir,
            )
            diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest() if diff_text else None
            changed_files = _extract_changed_files(
                ws_handle.workspace_path,
                target_revision=case.target_repository_revision,
                trusted_git_dir=ws_handle.trusted_git_dir,
            )

            exec_identity, identity_status = self._resolve_execution_identity(
                task_id, terminal_snap
            )
            terminal_event_type, terminal_ts = _derive_terminal_event_and_ts(
                terminal_snap, finish_time, pending_interaction
            )
            failure_kind = _extract_failure_kind(terminal_snap)
            evidence = _extract_candidate_evidence(terminal_snap)

            return CandidateTaskExecutionResult(
                task_id=task_id,
                workspace_handle=ws_handle,
                task_status=terminal_snap.status,
                terminal_event_type=terminal_event_type,
                terminal_timestamp=terminal_ts,
                failure_kind=failure_kind,
                diff_sha256=diff_sha,
                diff_text=diff_text,
                changed_files=changed_files,
                execution_identity=exec_identity,
                execution_identity_status=identity_status,
                time_to_terminal_seconds=elapsed_sec,
                pending_interaction=pending_interaction,
                in_task_repairs=evidence["in_task_repairs"],
                verification_events=evidence["verification_events"],
                review_artifacts=evidence["review_artifacts"],
                timeline_events=evidence["timeline_events"],
                artifacts=evidence["artifacts"],
                task_spec=terminal_snap.task_spec,
                constraints=terminal_snap.constraints,
            )
        finally:
            self.memory_isolation_manager.discard_case_memory_writes()

    def capture_terminal_snapshot(
        self, case: M29MutationCase, cand_res: CandidateTaskExecutionResult
    ) -> TerminalSnapshot:
        """Re-query API task state and re-hash workspace diff to verify immutability."""
        client = self._get_client()
        resp = client.get(
            f"{self.api_base_url}/tasks/{cand_res.task_id}",
            headers=self._get_headers(),
        )
        if resp.status_code != 200:
            msg = f"Failed to fetch task state: HTTP {resp.status_code}: {resp.text[:100]}"
            raise RuntimeError(msg)
        snap = TaskSnapshot.model_validate(resp.json())
        task_status = snap.status
        failure_kind = _extract_failure_kind(snap)
        terminal_event_type, terminal_ts = _derive_terminal_event_and_ts(
            snap, cand_res.terminal_timestamp, cand_res.pending_interaction
        )

        diff_text = _extract_workspace_diff(
            cand_res.workspace_handle.workspace_path,
            target_revision=case.target_repository_revision,
            trusted_git_dir=cand_res.workspace_handle.trusted_git_dir,
        )
        diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest() if diff_text else None
        changed_files = _extract_changed_files(
            cand_res.workspace_handle.workspace_path,
            target_revision=case.target_repository_revision,
            trusted_git_dir=cand_res.workspace_handle.trusted_git_dir,
        )

        return create_terminal_snapshot(
            task_status=task_status,
            terminal_event_type=terminal_event_type,
            terminal_timestamp=terminal_ts,
            failure_kind=failure_kind,
            diff_sha256=diff_sha,
            changed_files=changed_files,
        )


__all__ = [
    "DisposableDbMemoryIsolationManager",
    "HttpStackCandidateDispatcher",
]

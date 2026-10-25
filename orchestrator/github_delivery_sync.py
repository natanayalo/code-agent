"""Delivery synchronization handlers for GitHub webhooks (M30)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from db.enums import TimelineEventType
from db.models import Task, WorkerRun
from orchestrator.execution import TaskExecutionService
from orchestrator.github_repo import github_repo_spec_from_url
from repositories import InboundDeliveryRepository, TaskTimelineRepository, session_scope

logger = logging.getLogger(__name__)


def extract_delivery_identifiers(
    payload: dict[str, Any], event: str
) -> tuple[int | None, str | None, str | None]:
    """Extract pr_number, head_sha, and branch_name from a delivery webhook payload."""
    if event == "pull_request":
        pr = payload.get("pull_request") or {}
        return (
            pr.get("number"),
            (pr.get("head") or {}).get("sha"),
            (pr.get("head") or {}).get("ref"),
        )
    if event == "check_run":
        check_run = payload.get("check_run") or {}
        check_suite = check_run.get("check_suite") or {}
        prs = check_run.get("pull_requests") or check_suite.get("pull_requests") or []
        return (
            prs[0].get("number") if prs else None,
            check_run.get("head_sha"),
            check_suite.get("head_branch"),
        )
    if event == "check_suite":
        check_suite = payload.get("check_suite") or {}
        prs = check_suite.get("pull_requests") or []
        return (
            prs[0].get("number") if prs else None,
            check_suite.get("head_sha"),
            check_suite.get("head_branch"),
        )
    return None, None, None


def sync_pr_metadata(meta: dict[str, Any], pr: dict[str, Any]) -> None:
    """Update delivery_metadata dictionary from a pull_request payload."""
    meta["pr_number"] = pr.get("number")
    meta["pr_url"] = pr.get("html_url")
    meta["pr_node_id"] = pr.get("node_id")
    meta["pr_state"] = "merged" if pr.get("merged") else pr.get("state")
    meta["pr_draft"] = pr.get("draft", False)
    if head_sha := (pr.get("head") or {}).get("sha"):
        if meta.get("head_sha") and meta["head_sha"] != head_sha:
            meta["check_runs"] = [
                c
                for c in (meta.get("check_runs") or [])
                if not c.get("head_sha") or c.get("head_sha") == head_sha
            ]
        meta["head_sha"] = head_sha
    if branch_name := (pr.get("head") or {}).get("ref"):
        meta["branch_name"] = branch_name
    if base_branch := (pr.get("base") or {}).get("ref"):
        meta["base_branch"] = base_branch


def build_check_entry(payload: dict[str, Any], event: str, head_sha: str | None) -> dict[str, Any]:
    """Build a normalized check entry dictionary for a check_run or check_suite."""
    if event == "check_run":
        cr = payload.get("check_run") or {}
        return {
            "id": cr.get("id"),
            "name": cr.get("name") or "unknown",
            "status": cr.get("status") or "unknown",
            "conclusion": cr.get("conclusion"),
            "html_url": cr.get("html_url"),
            "head_sha": head_sha,
        }
    cs = payload.get("check_suite") or {}
    return {
        "id": cs.get("id"),
        "name": (cs.get("app") or {}).get("name") or "Check Suite",
        "status": cs.get("status") or "unknown",
        "conclusion": cs.get("conclusion"),
        "html_url": cs.get("url"),
        "head_sha": head_sha,
    }


def upsert_check_run(meta: dict[str, Any], check_entry: dict[str, Any]) -> None:
    """Upsert a check run into delivery_metadata and update aggregate CI status."""
    head_sha = check_entry.get("head_sha") or meta.get("head_sha")
    if head_sha and meta.get("head_sha") != head_sha:
        meta["head_sha"] = head_sha

    raw_check_runs = list(meta.get("check_runs") or [])
    if head_sha:
        raw_check_runs = [
            c for c in raw_check_runs if not c.get("head_sha") or c.get("head_sha") == head_sha
        ]

    check_runs = raw_check_runs
    updated = False
    for i, existing in enumerate(check_runs):
        same_id = existing.get("id") is not None and existing.get("id") == check_entry.get("id")
        same_name = bool(existing.get("name") and existing.get("name") == check_entry.get("name"))
        if same_id or same_name:
            check_runs[i] = check_entry
            updated = True
            break
    if not updated:
        check_runs.append(check_entry)
    meta["check_runs"] = check_runs

    failed_jobs = [
        c.get("name")
        for c in check_runs
        if c.get("conclusion") in {"failure", "timed_out", "action_required", "cancelled"}
        and c.get("name")
    ]
    if failed_jobs:
        ci_status = "failed"
    elif any(
        c.get("status") in {"queued", "in_progress"} or c.get("conclusion") is None
        for c in check_runs
    ):
        ci_status = "pending"
    elif any(c.get("conclusion") in {"success", "neutral", "skipped"} for c in check_runs):
        ci_status = "passed"
    else:
        ci_status = "pending"

    meta["ci_status"] = ci_status
    meta["ci_failed_jobs"] = failed_jobs
    meta["ci_last_checked_at"] = datetime.now(UTC).isoformat()


def find_matching_delivery_run(
    session: Session,
    repo_url: str,
    pr_number: int | None,
    head_sha: str | None,
    branch_name: str | None,
) -> tuple[WorkerRun | None, Task | None]:
    """Find the latest matching WorkerRun and Task for an allowlisted repo."""
    repo_spec = github_repo_spec_from_url(repo_url)
    if not repo_spec:
        return None, None

    run_stmt = (
        select(WorkerRun)
        .join(WorkerRun.task)
        .options(selectinload(WorkerRun.task))
        .where(
            WorkerRun.delivery_metadata.is_not(None),
            Task.repo_url.ilike(f"%{repo_spec}%"),
        )
        .order_by(WorkerRun.started_at.desc(), WorkerRun.id.desc())
        .limit(100)
    )
    branch_matches: list[WorkerRun] = []
    for run in session.scalars(run_stmt):
        if not run.task or github_repo_spec_from_url(run.task.repo_url) != repo_spec:
            continue
        meta = run.delivery_metadata or {}
        if pr_number is not None and meta.get("pr_number") == pr_number:
            return run, run.task
        if head_sha is not None and meta.get("head_sha") == head_sha:
            return run, run.task
        if branch_name is not None and meta.get("branch_name") == branch_name:
            branch_matches.append(run)

    if branch_matches:
        return branch_matches[0], branch_matches[0].task

    task_stmt = (
        select(Task)
        .options(selectinload(Task.worker_runs))
        .where(Task.repo_url.ilike(f"%{repo_spec}%"))
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(50)
    )
    for task in session.scalars(task_stmt):
        if github_repo_spec_from_url(task.repo_url) != repo_spec:
            continue
        delivery_branch = (task.constraints or {}).get("delivery_branch")
        if branch_name is not None and (
            task.branch == branch_name or delivery_branch == branch_name
        ):
            latest_run = (
                max(task.worker_runs, key=lambda r: (r.started_at, r.id))
                if task.worker_runs
                else None
            )
            return latest_run, task

    return None, None


def record_delivery_timeline_event(
    *,
    session: Session,
    task: Task,
    event: str,
    action: str | None,
    delivery_id: str,
    pr_number: int | None,
    head_sha: str | None,
    branch_name: str | None,
    worker_run: WorkerRun | None,
    check_entry: dict[str, Any] | None,
    pr_payload: dict[str, Any],
) -> None:
    """Record a deduplicated delivery timeline event on the task."""
    meta = worker_run.delivery_metadata or {} if worker_run else {}
    if event == "pull_request":
        event_type = TimelineEventType.DELIVERY_COMPLETED
        msg = f"GitHub pull request #{pr_number} ({meta.get('pr_state', 'open')})"
        timeline_payload = {
            "event": "pull_request",
            "action": action,
            "pr_number": pr_number,
            "pr_url": meta.get("pr_url") or pr_payload.get("html_url"),
            "pr_state": meta.get("pr_state") or pr_payload.get("state"),
            "pr_draft": meta.get("pr_draft", False),
            "head_sha": head_sha,
            "branch": branch_name,
        }
    else:
        conclusion = check_entry.get("conclusion") if check_entry else None
        is_failed = conclusion in {"failure", "timed_out", "action_required", "cancelled"}
        event_type = (
            TimelineEventType.DELIVERY_FAILED if is_failed else TimelineEventType.DELIVERY_COMPLETED
        )
        name = check_entry.get("name", "check") if check_entry else "check"
        status_str = check_entry.get("status", "unknown") if check_entry else "unknown"
        msg = f"GitHub {event.replace('_', ' ')} '{name}' ({status_str}/{conclusion or 'pending'})"
        timeline_payload = {
            "event": event,
            "action": action,
            "check_run": check_entry,
            "ci_status": meta.get("ci_status"),
            "ci_failed_jobs": meta.get("ci_failed_jobs", []),
        }
        if pr_number is not None:
            timeline_payload["pr_number"] = pr_number
        if head_sha is not None:
            timeline_payload["head_sha"] = head_sha

    source_issue_number = meta.get("source_issue_number")
    source_issue_url = meta.get("source_issue_url")
    if (
        (source_issue_number is None or not source_issue_url)
        and isinstance(task.constraints, dict)
        and isinstance(task.constraints.get("github"), dict)
    ):
        gh_constraints = task.constraints["github"]
        if source_issue_number is None:
            source_issue_number = gh_constraints.get("issue_number")
        if not source_issue_url:
            source_issue_url = gh_constraints.get("issue_url")

    if source_issue_number is not None:
        timeline_payload["source_issue_number"] = source_issue_number
    if source_issue_url:
        timeline_payload["source_issue_url"] = source_issue_url

    timeline_repo = TaskTimelineRepository(session)
    timeline_repo.create_next_for_attempt(
        task_id=task.id,
        attempt_number=task.attempt_count,
        event_type=event_type,
        event_key=f"github_delivery:{delivery_id}",
        message=msg,
        payload=timeline_payload,
    )


def _apply_worker_run_delivery_update(
    worker_run: WorkerRun | None,
    event: str,
    payload: dict[str, Any],
    head_sha: str | None,
    task: Task | None = None,
) -> dict[str, Any] | None:
    """Update worker_run delivery metadata and return check_entry if applicable."""
    if worker_run is None:
        return None
    meta = dict(worker_run.delivery_metadata or {})
    target_task = task or getattr(worker_run, "task", None)
    if target_task and isinstance(target_task.constraints, dict):
        gh_constraints = target_task.constraints.get("github")
        if isinstance(gh_constraints, dict):
            issue_num = gh_constraints.get("issue_number")
            if issue_num is not None and not meta.get("source_issue_number"):
                meta["source_issue_number"] = issue_num
            issue_url = gh_constraints.get("issue_url")
            if issue_url and not meta.get("source_issue_url"):
                meta["source_issue_url"] = issue_url
    check_entry: dict[str, Any] | None = None
    if event == "pull_request":
        sync_pr_metadata(meta, payload.get("pull_request") or {})
    else:
        check_entry = build_check_entry(payload, event, head_sha)
        upsert_check_run(meta, check_entry)
    worker_run.delivery_metadata = meta
    flag_modified(worker_run, "delivery_metadata")
    return check_entry


def handle_delivery_sync(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    task_service: TaskExecutionService,
) -> tuple[bool, str, bool, str | None, str | None, str | None]:
    """Synchronize pull_request and check_run/check_suite events onto WorkerRun.

    Returns:
        (ok, status, duplicate, task_id, run_id, detail)
    """
    with session_scope(task_service.session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)
        existing = delivery_repo.get_by_channel_delivery(channel="github", delivery_id=delivery_id)
        if existing is not None:
            return (
                True,
                "duplicate",
                True,
                existing.task_id,
                None,
                "Duplicate delivery deduplicated.",
            )

        pr_number, head_sha, branch_name = extract_delivery_identifiers(payload, event)
        worker_run, task = find_matching_delivery_run(
            session=session,
            repo_url=repo_url,
            pr_number=pr_number,
            head_sha=head_sha,
            branch_name=branch_name,
        )

        if task is None:
            try:
                with session.begin_nested():
                    delivery_repo.create(channel="github", delivery_id=delivery_id, task_id=None)
            except IntegrityError:
                pass
            return True, "ignored", False, None, None, "No matching code-agent task found."

        check_entry = _apply_worker_run_delivery_update(
            worker_run,
            event,
            payload,
            head_sha,
            task=task,
        )

        record_delivery_timeline_event(
            session=session,
            task=task,
            event=event,
            action=action,
            delivery_id=delivery_id,
            pr_number=pr_number,
            head_sha=head_sha,
            branch_name=branch_name,
            worker_run=worker_run,
            check_entry=check_entry,
            pr_payload=payload.get("pull_request") or {},
        )

        try:
            with session.begin_nested():
                delivery_repo.create(channel="github", delivery_id=delivery_id, task_id=task.id)
        except IntegrityError:
            pass

        return (
            True,
            "synced",
            False,
            task.id,
            worker_run.id if worker_run else None,
            f"Synchronized {event} event onto task {task.id}.",
        )

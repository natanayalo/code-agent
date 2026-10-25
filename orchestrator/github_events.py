"""Domain models and processing logic for GitHub webhook events (M30)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from apps.api.config import SystemConfig
from orchestrator.execution import TaskExecutionService
from orchestrator.github_delivery_sync import (
    build_check_entry,
    extract_delivery_identifiers,
    find_matching_delivery_run,
    handle_delivery_sync,
    record_delivery_timeline_event,
    sync_pr_metadata,
    upsert_check_run,
)
from orchestrator.github_issue_intake import (
    check_issue_event_eligibility,
    extract_code_agent_command,
    handle_issue_intake,
    is_authorized_operator,
    synthesize_issue_task_text,
)

logger = logging.getLogger(__name__)

SUPPORTED_GITHUB_EVENTS = frozenset(
    {"ping", "issues", "issue_comment", "pull_request", "check_run", "check_suite"}
)


class GitHubWebhookResult(BaseModel):
    """Structured outcome of a processed GitHub webhook event."""

    ok: bool
    status: str
    event: str
    action: str | None = None
    delivery_id: str
    duplicate: bool = False
    task_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    detail: str | None = None


def _handle_issue_result(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    config: SystemConfig,
    task_service: TaskExecutionService,
) -> GitHubWebhookResult:
    ok, status_val, duplicate, task_id, session_id, detail = handle_issue_intake(
        payload=payload,
        event=event,
        action=action,
        delivery_id=delivery_id,
        repo_url=repo_url,
        config=config,
        task_service=task_service,
    )
    return GitHubWebhookResult(
        ok=ok,
        status=status_val,
        event=event,
        action=action,
        delivery_id=delivery_id,
        duplicate=duplicate,
        task_id=task_id,
        session_id=session_id,
        detail=detail,
    )


def _handle_delivery_result(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    task_service: TaskExecutionService,
) -> GitHubWebhookResult:
    ok, status_val, duplicate, task_id, run_id, detail = handle_delivery_sync(
        payload=payload,
        event=event,
        action=action,
        delivery_id=delivery_id,
        repo_url=repo_url,
        task_service=task_service,
    )
    return GitHubWebhookResult(
        ok=ok,
        status=status_val,
        event=event,
        action=action,
        delivery_id=delivery_id,
        duplicate=duplicate,
        task_id=task_id,
        run_id=run_id,
        detail=detail,
    )


def process_github_webhook_event(
    *,
    payload: dict[str, Any],
    event: str,
    delivery_id: str,
    config: SystemConfig,
    task_service: TaskExecutionService,
) -> GitHubWebhookResult:
    """Master dispatcher for incoming GitHub webhook events."""
    action = payload.get("action")
    if event == "ping":
        return GitHubWebhookResult(
            ok=True,
            status="accepted",
            event=event,
            action=action,
            delivery_id=delivery_id,
            detail="pong",
        )

    if event not in SUPPORTED_GITHUB_EVENTS:
        return GitHubWebhookResult(
            ok=True,
            status="ignored",
            event=event,
            action=action,
            delivery_id=delivery_id,
            detail=f"Unhandled event type: {event}",
        )

    repository = payload.get("repository") or {}
    repo_identifier = (
        repository.get("full_name") or repository.get("html_url") or repository.get("clone_url")
    )
    repo_url = config.resolve_github_repo_url(repo_identifier)
    if not repo_url:
        return GitHubWebhookResult(
            ok=False,
            status="repo_not_allowed",
            event=event,
            action=action,
            delivery_id=delivery_id,
            detail=f"Repository '{repo_identifier}' is not in allowed_repos.",
        )

    if event in {"issues", "issue_comment"}:
        return _handle_issue_result(
            payload=payload,
            event=event,
            action=action,
            delivery_id=delivery_id,
            repo_url=repo_url,
            config=config,
            task_service=task_service,
        )

    return _handle_delivery_result(
        payload=payload,
        event=event,
        action=action,
        delivery_id=delivery_id,
        repo_url=repo_url,
        task_service=task_service,
    )


__all__ = [
    "GitHubWebhookResult",
    "SUPPORTED_GITHUB_EVENTS",
    "build_check_entry",
    "check_issue_event_eligibility",
    "extract_code_agent_command",
    "extract_delivery_identifiers",
    "find_matching_delivery_run",
    "handle_delivery_sync",
    "handle_issue_intake",
    "is_authorized_operator",
    "process_github_webhook_event",
    "record_delivery_timeline_event",
    "sync_pr_metadata",
    "synthesize_issue_task_text",
    "upsert_check_run",
]

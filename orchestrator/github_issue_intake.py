"""Issue and issue_comment webhook intake handlers for GitHub-native tasks (M30)."""

from __future__ import annotations

import logging
import re
from typing import Any

from apps.api.config import SystemConfig
from db.enums import TimelineEventType
from orchestrator.execution import (
    DeliveryKey,
    SubmissionSession,
    TaskExecutionService,
    TaskSubmission,
)
from orchestrator.github_repo import github_repo_spec_from_url
from repositories import TaskTimelineRepository, session_scope

logger = logging.getLogger(__name__)


_CODE_AGENT_CMD_PATTERN = re.compile(r"(?:^|\s)/code-agent(?=$|\s)", re.IGNORECASE)


def extract_code_agent_command(text: str) -> tuple[bool, str]:
    """Check if /code-agent is present as a standalone command token and extract instruction."""
    if not text:
        return False, ""
    match = _CODE_AGENT_CMD_PATTERN.search(text)
    if not match:
        return False, ""
    instruction = text[match.end() :].strip()
    return True, instruction


def is_authorized_operator(
    *,
    sender_login: str,
    author_association: str | None,
    repo_owner_login: str | None,
    config_owner_login: str | None,
    is_comment: bool = False,
) -> bool:
    """Validate that the event sender is authorized to trigger task execution."""
    if config_owner_login:
        return sender_login.lower() == config_owner_login.strip().lower()
    if is_comment:
        return (author_association or "").upper() in {"OWNER", "MEMBER", "COLLABORATOR"}
    return bool(repo_owner_login and sender_login.lower() == repo_owner_login.lower())


def synthesize_issue_task_text(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str | None,
    instruction: str | None = None,
) -> str:
    """Synthesize task text from issue title, body, and explicit instruction."""
    parts = [f"Issue #{issue_number}: {issue_title}"]
    body_clean = (issue_body or "").strip()
    if body_clean:
        parts.append(body_clean)
    instruction_clean = (instruction or "").strip()
    if instruction_clean:
        parts.append(f"Instruction:\n{instruction_clean}")
    full_text = "\n\n".join(parts)
    return full_text[:10_000]


def check_issue_event_eligibility(
    payload: dict[str, Any],
    event: str,
    action: str | None,
    config: SystemConfig,
) -> tuple[bool, str | None, str | None]:
    """Evaluate whether an issues or issue_comment event meets trigger conditions."""
    issue = payload.get("issue") or {}
    if event == "issue_comment":
        if issue.get("pull_request") is not None:
            return False, "pull_request_comment", None
        if action != "created":
            return False, f"Unhandled issue_comment action: {action}", None
        comment = payload.get("comment") or {}
        has_cmd, instruction = extract_code_agent_command(comment.get("body") or "")
        if not has_cmd:
            return False, "no_trigger_command", None
        return True, None, instruction

    if event == "issues":
        if action == "opened":
            has_cmd, instruction = extract_code_agent_command(issue.get("body") or "")
            label_match = any(
                (lbl.get("name") or "").lower() == config.github_issue_label.lower()
                for lbl in issue.get("labels") or []
            )
            if not has_cmd and not label_match:
                return False, "no_matching_trigger", None
            return True, None, instruction
        if action == "labeled":
            added_label = (payload.get("label") or {}).get("name") or ""
            if added_label.lower() != config.github_issue_label.lower():
                return False, "no_matching_trigger", None
            return True, None, None
        if action == "assigned":
            if not config.github_owner_login:
                return False, "no_matching_trigger", None
            assignee = (payload.get("assignee") or {}).get("login") or ""
            if assignee.lower() != config.github_owner_login.lower():
                return False, "no_matching_trigger", None
            return True, None, None
        return False, f"Unhandled issues action: {action}", None

    return False, f"Unhandled event: {event}", None


def _build_issue_submission(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    sender_login: str,
    instruction: str | None,
) -> tuple[TaskSubmission, dict[str, Any], int]:
    """Build canonical TaskSubmission and github constraints for issue intake."""
    issue = payload.get("issue") or {}
    issue_number = int(issue.get("number") or 0)
    issue_id = issue.get("id")
    issue_node_id = issue.get("node_id")
    issue_url = issue.get("html_url")
    issue_title = str(issue.get("title") or "")

    task_text = synthesize_issue_task_text(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue.get("body"),
        instruction=instruction,
    )
    delivery_branch = f"github/issue-{issue_number}"
    pr_title = f"{issue_title} (#{issue_number})"
    pr_body = f"Addresses #{issue_number}\n\nAutomated draft PR generated by code-agent."
    repo_spec = github_repo_spec_from_url(repo_url)

    github_constraints: dict[str, Any] = {
        "delivery_id": delivery_id,
        "issue_number": issue_number,
        "issue_id": issue_id,
        "issue_node_id": issue_node_id,
        "issue_url": issue_url,
        "issue_title": issue_title,
        "sender_login": sender_login,
        "trigger_type": f"{event}:{action}",
    }
    if event == "issue_comment":
        comment = payload.get("comment") or {}
        github_constraints["comment_id"] = comment.get("id")

    constraints: dict[str, Any] = {
        "github": github_constraints,
        "delivery_mode": "draft_pr",
        "delivery_branch": delivery_branch,
        "pr_title": pr_title,
        "pr_body": pr_body,
    }
    submission = TaskSubmission(
        task_text=task_text,
        repo_url=repo_url,
        constraints=constraints,
        session=SubmissionSession(
            channel="github",
            external_user_id=f"github:user:{sender_login.lower()}",
            external_thread_id=f"github:{repo_spec}#issue:{issue_number}",
            display_name=sender_login,
        ),
    )
    return submission, github_constraints, issue_number


def _record_ingested_timeline_event(
    *,
    task_service: TaskExecutionService,
    task_id: str,
    issue_number: int,
    event: str,
    action: str | None,
    delivery_id: str,
    github_constraints: dict[str, Any],
) -> None:
    """Record a deduplicated TASK_INGESTED timeline event for issue intake."""
    with session_scope(task_service.session_factory) as session:
        timeline_repo = TaskTimelineRepository(session)
        timeline_repo.create_next_for_attempt(
            task_id=task_id,
            attempt_number=0,
            event_type=TimelineEventType.TASK_INGESTED,
            event_key=f"github_delivery:{delivery_id}",
            message=f"Ingested GitHub issue #{issue_number} event ({event}:{action})",
            payload=github_constraints,
        )


def _validate_operator_authorization(
    payload: dict[str, Any],
    event: str,
    config: SystemConfig,
) -> tuple[bool, str]:
    """Check if the sender is authorized under system and repo configuration."""
    sender = payload.get("sender") or {}
    sender_login = sender.get("login") or ""
    comment = payload.get("comment") or {}
    author_association = comment.get("author_association") if event == "issue_comment" else None
    repo_owner = (payload.get("repository") or {}).get("owner", {}).get("login")
    authorized = is_authorized_operator(
        sender_login=sender_login,
        author_association=author_association,
        repo_owner_login=repo_owner,
        config_owner_login=config.github_owner_login,
        is_comment=(event == "issue_comment"),
    )
    return authorized, sender_login


def handle_issue_intake(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    config: SystemConfig,
    task_service: TaskExecutionService,
) -> tuple[bool, str, bool, str | None, str | None, str | None]:
    """Process an issues or issue_comment event.

    Returns:
        (ok, status, duplicate, task_id, session_id, detail)
    """
    authorized, sender_login = _validate_operator_authorization(payload, event, config)
    if not authorized:
        return (
            False,
            "unauthorized",
            False,
            None,
            None,
            f"User '{sender_login}' is not authorized to trigger tasks.",
        )

    eligible, reject_reason, instruction = check_issue_event_eligibility(
        payload, event, action, config
    )
    if not eligible:
        return True, "ignored", False, None, None, reject_reason

    submission, github_constraints, issue_number = _build_issue_submission(
        payload=payload,
        event=event,
        action=action,
        delivery_id=delivery_id,
        repo_url=repo_url,
        sender_login=sender_login,
        instruction=instruction,
    )

    outcome = task_service.create_task_outcome(
        submission,
        delivery_key=DeliveryKey(channel="github", delivery_id=delivery_id),
    )
    if outcome.duplicate:
        return (
            True,
            "duplicate",
            True,
            outcome.task_snapshot.task_id,
            outcome.task_snapshot.session_id,
            "Duplicate delivery deduplicated.",
        )

    task_id = outcome.task_snapshot.task_id
    _record_ingested_timeline_event(
        task_service=task_service,
        task_id=task_id,
        issue_number=issue_number,
        event=event,
        action=action,
        delivery_id=delivery_id,
        github_constraints=github_constraints,
    )

    return (
        True,
        "accepted",
        False,
        task_id,
        outcome.task_snapshot.session_id,
        f"Created task from {event}:{action} event.",
    )

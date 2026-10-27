"""Issue and issue_comment webhook intake handlers for GitHub-native tasks (M30)."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from apps.api.config import SystemConfig
from db.enums import TimelineEventType
from orchestrator.execution import (
    DeliveryKey,
    SubmissionSession,
    TaskExecutionService,
    TaskSubmission,
)
from orchestrator.github_repo import github_repo_spec_from_url
from repositories import (
    InboundDeliveryRepository,
    SessionRepository,
    TaskRepository,
    TaskTimelineRepository,
    session_scope,
)

logger = logging.getLogger(__name__)


_CODE_AGENT_CMD_PATTERN = re.compile(r"(?:^|\s)/code-agent(?=$|\s)", re.IGNORECASE)
_REPLAY_COMMAND_PATTERN = re.compile(r"^replay$", re.IGNORECASE)


def parse_issue_command_intent(instruction: str | None) -> tuple[str, str | None]:
    """Distinguish replay command intent from regular task instruction.

    Returns:
        (intent, remaining_instruction)
        where intent is 'replay' or 'create'.
    """
    if not instruction:
        return "create", None
    trimmed = instruction.strip()
    if not trimmed:
        return "create", None
    if _REPLAY_COMMAND_PATTERN.match(trimmed):
        return "replay", None
    return "create", trimmed


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


def _check_existing_delivery(
    task_service: TaskExecutionService, delivery_id: str
) -> tuple[str, str | None] | None:
    """Return task_id and session_id if this delivery was already recorded."""
    with session_scope(task_service.session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)
        existing = delivery_repo.get_by_channel_delivery(channel="github", delivery_id=delivery_id)
        if existing is not None and existing.task_id is not None:
            task = TaskRepository(session).get(existing.task_id)
            session_id = task.session_id if task else None
            return existing.task_id, session_id
    return None


def _resolve_issue_latest_task(
    task_service: TaskExecutionService, repo_url: str, issue_number: int
) -> tuple[str | None, str | None, str | None]:
    """Find the latest task and session ID for an issue thread.

    Returns:
        (latest_task_id, session_id, error_detail)
    """
    repo_spec = github_repo_spec_from_url(repo_url)
    external_thread_id = f"github:{repo_spec}#issue:{issue_number}"
    with session_scope(task_service.session_factory) as session:
        conv = SessionRepository(session).get_by_channel_thread(
            channel="github", external_thread_id=external_thread_id
        )
        if conv is None:
            return None, None, "No prior task found for this GitHub issue to replay."
        tasks = TaskRepository(session).list_by_session(conv.id)
        if not tasks:
            return None, conv.id, "No prior task found for this GitHub issue to replay."
        return tasks[-1].id, conv.id, None


def _link_replayed_delivery_and_timeline(
    *,
    task_service: TaskExecutionService,
    new_task_id: str,
    latest_task_id: str,
    delivery_id: str,
    issue_number: int,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    sender_login: str,
) -> None:
    """Attach inbound delivery record and emit TASK_INGESTED timeline event for replay."""
    with session_scope(task_service.session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)
        existing = delivery_repo.get_by_channel_delivery(channel="github", delivery_id=delivery_id)
        if existing is None:
            try:
                with session.begin_nested():
                    delivery_repo.create(
                        channel="github", delivery_id=delivery_id, task_id=new_task_id
                    )
            except IntegrityError:
                pass
        elif existing.task_id is None:
            delivery_repo.attach_task_if_unassigned(
                channel="github", delivery_id=delivery_id, task_id=new_task_id
            )

    issue = payload.get("issue") or {}
    github_constraints: dict[str, Any] = {
        "delivery_id": delivery_id,
        "issue_number": issue_number,
        "issue_id": issue.get("id"),
        "issue_node_id": issue.get("node_id"),
        "issue_url": issue.get("html_url"),
        "issue_title": str(issue.get("title") or ""),
        "sender_login": sender_login,
        "trigger_type": f"{event}:{action}",
        "replayed_from": latest_task_id,
    }
    if event == "issue_comment":
        comment = payload.get("comment") or {}
        github_constraints["comment_id"] = comment.get("id")

    _record_ingested_timeline_event(
        task_service=task_service,
        task_id=new_task_id,
        issue_number=issue_number,
        event=event,
        action=action,
        delivery_id=delivery_id,
        github_constraints=github_constraints,
    )


def _handle_issue_replay(
    *,
    payload: dict[str, Any],
    event: str,
    action: str | None,
    delivery_id: str,
    repo_url: str,
    sender_login: str,
    task_service: TaskExecutionService,
) -> tuple[bool, str, bool, str | None, str | None, str | None]:
    """Replay the latest terminal task for the issue conversation session."""
    existing = _check_existing_delivery(task_service, delivery_id)
    if existing is not None:
        return True, "duplicate", True, existing[0], existing[1], "Duplicate delivery deduplicated."

    issue = payload.get("issue") or {}
    issue_number = int(issue.get("number") or 0)
    latest_task_id, session_id, err = _resolve_issue_latest_task(
        task_service, repo_url, issue_number
    )
    if latest_task_id is None:
        return False, "not_found", False, None, session_id, err

    delivery_key = DeliveryKey(channel="github", delivery_id=delivery_id)
    replay_result = task_service.replay_task(
        source_task_id=latest_task_id, delivery_key=delivery_key
    )
    if replay_result.status == "duplicate":
        assert replay_result.task_snapshot is not None
        return (
            True,
            "duplicate",
            True,
            replay_result.task_snapshot.task_id,
            replay_result.task_snapshot.session_id,
            "Duplicate delivery deduplicated.",
        )
    if replay_result.status != "created":
        return (
            False,
            replay_result.status,
            False,
            None,
            session_id,
            replay_result.detail or f"Replay failed with status: {replay_result.status}",
        )

    assert replay_result.task_snapshot is not None
    new_task_id = replay_result.task_snapshot.task_id
    new_session_id = replay_result.task_snapshot.session_id

    _link_replayed_delivery_and_timeline(
        task_service=task_service,
        new_task_id=new_task_id,
        latest_task_id=latest_task_id,
        delivery_id=delivery_id,
        issue_number=issue_number,
        payload=payload,
        event=event,
        action=action,
        sender_login=sender_login,
    )
    return (
        True,
        "accepted",
        False,
        new_task_id,
        new_session_id,
        f"Replayed task {latest_task_id} from {event}:{action} event.",
    )


def _dispatch_issue_submission(
    *,
    task_service: TaskExecutionService,
    submission: TaskSubmission,
    github_constraints: dict[str, Any],
    issue_number: int,
    event: str,
    action: str | None,
    delivery_id: str,
) -> tuple[bool, str, bool, str | None, str | None, str | None]:
    """Persist new task submission or return duplicate delivery snapshot."""
    outcome = task_service.create_task_outcome(
        submission, delivery_key=DeliveryKey(channel="github", delivery_id=delivery_id)
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

    intent, _ = parse_issue_command_intent(instruction)
    if intent == "replay":
        return _handle_issue_replay(
            payload=payload,
            event=event,
            action=action,
            delivery_id=delivery_id,
            repo_url=repo_url,
            sender_login=sender_login,
            task_service=task_service,
        )

    submission, github_constraints, issue_number = _build_issue_submission(
        payload=payload,
        event=event,
        action=action,
        delivery_id=delivery_id,
        repo_url=repo_url,
        sender_login=sender_login,
        instruction=instruction,
    )
    return _dispatch_issue_submission(
        task_service=task_service,
        submission=submission,
        github_constraints=github_constraints,
        issue_number=issue_number,
        event=event,
        action=action,
        delivery_id=delivery_id,
    )

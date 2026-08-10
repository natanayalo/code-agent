"""GitHub review-comment REST API integration, prompt building, and polling helpers."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from db.enums import TaskStatus
from db.models import Session, Task, WorkerRun
from orchestrator.execution_types import DeliveryKey, SubmissionSession, TaskSubmission
from repositories import session_scope

logger = logging.getLogger(__name__)


class ReviewComment(BaseModel):
    """Structured representation of a GitHub pull request review comment."""

    id: int
    path: str
    line: int | None = None
    original_line: int | None = None
    body: str
    diff_hunk: str | None = None
    user_login: str
    created_at: str
    updated_at: str
    in_reply_to_id: int | None = None
    html_url: str = ""


def fetch_repo_owner(
    repo_spec: str,
    token: str,
    env: dict[str, str] | None = None,
) -> str | None:
    """Fetch the owner login for a GitHub repository, or use env override if set."""
    environ = env if env is not None else os.environ
    env_override = environ.get("CODE_AGENT_GITHUB_OWNER_LOGIN", "").strip()
    if env_override:
        return env_override

    if "/" not in repo_spec:
        return None

    owner, repository = repo_spec.split("/", 1)
    request_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    )
    request = Request(
        request_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "code-agent-github-reviews",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API host
            repo_info = json.load(response)
        if isinstance(repo_info, dict) and isinstance(repo_info.get("owner"), dict):
            return repo_info["owner"].get("login")
    except (HTTPError, json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Failed to fetch repo owner from GitHub API",
            extra={"repo_spec": repo_spec, "error": str(exc)},
        )
    return owner


def _parse_review_comment(item: dict[str, Any], owner_login: str | None) -> ReviewComment | None:
    if not isinstance(item, dict):
        return None
    user = item.get("user") or {}
    login = user.get("login") if isinstance(user, dict) else ""
    if owner_login and login != owner_login:
        return None
    comment_id = item.get("id")
    path = item.get("path")
    body = item.get("body")
    if not comment_id or not path or not body:
        return None

    return ReviewComment(
        id=int(comment_id),
        path=str(path),
        line=item.get("line"),
        original_line=item.get("original_line"),
        body=str(body),
        diff_hunk=item.get("diff_hunk"),
        user_login=str(login),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        in_reply_to_id=item.get("in_reply_to_id"),
        html_url=str(item.get("html_url") or ""),
    )


def fetch_review_comments(
    repo_spec: str,
    pr_number: int,
    token: str,
    since: str | None = None,
    owner_login: str | None = None,
) -> list[ReviewComment]:
    """Fetch review comments for a PR, optionally filtered by since timestamp and author."""
    if "/" not in repo_spec or pr_number <= 0:
        return []

    owner, repository = repo_spec.split("/", 1)
    comments: list[ReviewComment] = []
    page = 1

    while True:
        query_dict: dict[str, Any] = {"per_page": 100, "page": page}
        if since:
            query_dict["since"] = since
        query = urlencode(query_dict)

        request_url = (
            f"https://api.github.com/repos/{quote(owner, safe='')}/"
            f"{quote(repository, safe='')}/pulls/{pr_number}/comments?{query}"
        )
        request = Request(
            request_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "code-agent-github-reviews",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

        link_header = ""
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API host
                raw_comments = json.load(response)
                headers = getattr(response, "headers", {})
                link_header = headers.get("Link", "") if hasattr(headers, "get") else ""
        except (HTTPError, json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
            logger.warning(
                "Failed to fetch review comments from GitHub API",
                extra={"repo_spec": repo_spec, "pr_number": pr_number, "error": str(exc)},
            )
            break

        if not isinstance(raw_comments, list) or not raw_comments:
            break

        for item in raw_comments:
            comment = _parse_review_comment(item, owner_login)
            if comment:
                comments.append(comment)

        if 'rel="next"' not in link_header or len(raw_comments) < 100:
            break
        page += 1

    return comments


def reply_to_review_comment(
    repo_spec: str,
    pr_number: int,
    comment_id: int,
    body: str,
    token: str,
) -> bool:
    """Post a reply to an individual PR review comment thread."""
    if "/" not in repo_spec or pr_number <= 0 or comment_id <= 0:
        return False

    owner, repository = repo_spec.split("/", 1)
    request_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls/{pr_number}/comments/{comment_id}/replies"
    )
    request = Request(
        request_url,
        data=json.dumps({"body": body}).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "code-agent-github-reviews",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API host
            status_code = getattr(response, "status", 200)
            return status_code in (200, 201)
    except HTTPError as exc:
        logger.warning(
            "GitHub rejected review comment reply",
            extra={"repo_spec": repo_spec, "comment_id": comment_id, "status": exc.code},
        )
        return False
    except (json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Failed to post review comment reply",
            extra={"repo_spec": repo_spec, "comment_id": comment_id, "error": str(exc)},
        )
        return False


def build_review_comment_repair_prompt(comments: list[ReviewComment]) -> str:
    """Build a structured repair task prompt from a list of review comments."""
    if not comments:
        return "Review comments were left on the pull request. Please inspect and resolve them."

    prompt_lines = ["The operator left review comments on the pull request that require updates:\n"]
    by_file: dict[str, list[ReviewComment]] = {}
    for comment in comments:
        by_file.setdefault(comment.path, []).append(comment)

    for path, file_comments in by_file.items():
        prompt_lines.append(f"File: `{path}`")
        for c in file_comments:
            line_str = f"line {c.line}" if c.line is not None else "file review"
            prompt_lines.append(f"- Location: {line_str} (by @{c.user_login})")
            prompt_lines.append(f"  Comment: {c.body}")
            if c.diff_hunk:
                prompt_lines.append("  Context:")
                prompt_lines.append("  ```diff")
                prompt_lines.append(f"  {c.diff_hunk.strip()}")
                prompt_lines.append("  ```")
        prompt_lines.append("")

    prompt_lines.append(
        "Please address the review comments above. Make the necessary code updates, "
        "run verification tests, commit, and push the updates."
    )
    return "\n".join(prompt_lines)


def count_review_comment_repairs(session_factory: Any, parent_task_id: str) -> int:
    """Count how many review comment repair child tasks exist for a parent task."""
    if session_factory is None:
        return 0
    with session_scope(session_factory) as session:
        stmt = (
            select(Task)
            .join(Task.session)
            .where(
                Task.repair_for_task_id == parent_task_id,
                Session.channel == "review_comment_polling",
            )
        )
        return len(list(session.scalars(stmt)))


def update_run_review_comment_metadata(
    session_factory: Any,
    run_id: str,
    comments: list[ReviewComment],
) -> None:
    """Update WorkerRun delivery_metadata with review comment snapshots and timestamps."""
    if session_factory is None:
        return
    with session_scope(session_factory) as session:
        db_run = session.get(WorkerRun, run_id)
        if not db_run or not db_run.delivery_metadata:
            return
        meta = db_run.delivery_metadata
        existing_comments = meta.get("review_comments") or []
        comment_map = {c["id"]: c for c in existing_comments if isinstance(c, dict) and "id" in c}
        for c in comments:
            comment_map[c.id] = {
                "id": c.id,
                "path": c.path,
                "line": c.line,
                "body": c.body,
                "user_login": c.user_login,
                "created_at": c.created_at,
                "html_url": c.html_url,
                "replied": False,
            }
        meta["review_comments"] = list(comment_map.values())
        processed_ids = set(meta.get("processed_review_comment_ids") or [])
        for c in comments:
            processed_ids.add(c.id)
        meta["processed_review_comment_ids"] = list(processed_ids)
        meta["review_comments_last_checked_at"] = datetime.now(UTC).isoformat()
        flag_modified(db_run, "delivery_metadata")


def poll_review_comments(
    session_factory: Any,
    task_service: Any,
    config: Any,
    run_info: dict[str, Any],
    repo_spec: str,
    gh_token: str,
    owner_login: str | None = None,
) -> None:
    """Poll review comments for a run's PR and submit a repair task if new comments exist."""
    metadata = run_info.get("delivery_metadata") or {}
    pr_number = metadata.get("pr_number")
    if not pr_number:
        return

    resolved_owner = owner_login or fetch_repo_owner(repo_spec, gh_token)
    since = metadata.get("review_comments_last_checked_at")
    comments = fetch_review_comments(
        repo_spec, int(pr_number), gh_token, since=since, owner_login=resolved_owner
    )
    if not comments:
        return

    processed_ids = set(metadata.get("processed_review_comment_ids") or [])
    unprocessed = [c for c in comments if c.id not in processed_ids]
    if not unprocessed:
        return

    task_id = run_info["task_id"]
    repair_count = count_review_comment_repairs(session_factory, task_id)
    if repair_count >= config.review_comment_max_repair_passes:
        logger.info(
            "Review comment repair budget exhausted for task %s (max %s)",
            task_id,
            config.review_comment_max_repair_passes,
        )
        update_run_review_comment_metadata(session_factory, run_info["run_id"], unprocessed)
        return

    prompt = build_review_comment_repair_prompt(unprocessed)
    comment_ids = [c.id for c in unprocessed]
    latest_timestamp = max(c.created_at for c in unprocessed)
    delivery_key = DeliveryKey(
        channel="review_comment_polling",
        delivery_id=f"review_repair:{task_id}:{pr_number}:{latest_timestamp}",
    )
    submission = TaskSubmission(
        task_text=prompt,
        repo_url=run_info.get("repo_url"),
        branch=metadata.get("branch_name", ""),
        priority=10,
        repair_for_task_id=task_id,
        constraints={"review_comment_ids": comment_ids, "pr_number": pr_number},
        session=SubmissionSession(
            channel="review_comment_polling",
            external_user_id="system:review-comment-polling",
            external_thread_id=task_id,
            display_name="Review Comment Repair",
        ),
    )
    try:
        outcome = task_service.create_task_outcome(submission, delivery_key=delivery_key)
        if not outcome.duplicate:
            logger.info("Spawned review comment repair task %s", outcome.task_snapshot.task_id)
    except Exception as exc:
        logger.error("Failed to spawn review comment repair task: %s", exc)

    update_run_review_comment_metadata(session_factory, run_info["run_id"], unprocessed)


def _build_review_comment_reply_plan(
    session_factory: Any,
    task_id: str,
    replied_ids: set[int],
) -> list[tuple[int, str]]:
    """Query repair tasks and build reply bodies inside DB session scope."""
    plan_map: dict[int, str] = {}
    with session_scope(session_factory) as session:
        stmt = (
            select(Task)
            .options(selectinload(Task.worker_runs))
            .join(Task.session)
            .where(
                Task.repair_for_task_id == task_id,
                Session.channel == "review_comment_polling",
            )
        )
        repair_tasks = list(session.scalars(stmt))

        for repair_task in repair_tasks:
            if repair_task.status not in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                continue
            comment_ids = repair_task.constraints.get("review_comment_ids") or []
            unreplied = [cid for cid in comment_ids if cid not in replied_ids]
            if not unreplied:
                continue

            latest_run = repair_task.worker_runs[-1] if repair_task.worker_runs else None
            summary = (
                latest_run.summary
                if latest_run and latest_run.summary
                else repair_task.last_error or "Task completed."
            )

            if repair_task.status == TaskStatus.COMPLETED:
                body = (
                    "I've addressed these review comments in the latest commit.\n\n"
                    f"**Summary of changes:**\n{summary}"
                )
            else:
                body = (
                    "Attempted to address these review comments, but repair task failed.\n\n"
                    f"**Details:**\n{summary}"
                )

            for cid in unreplied:
                plan_map[cid] = body
    return list(plan_map.items())


def _record_review_comment_reply(
    session_factory: Any,
    run_id: str,
    cid: int,
    replied_ids: set[int],
) -> None:
    """Persist a single successfully sent review comment reply to DB delivery_metadata."""
    with session_scope(session_factory) as session:
        db_run = session.get(WorkerRun, run_id)
        if db_run and db_run.delivery_metadata:
            meta = db_run.delivery_metadata
            meta["replied_review_comment_ids"] = list(replied_ids)
            meta["review_comments_replied_at"] = datetime.now(UTC).isoformat()
            if isinstance(meta.get("review_comments"), list):
                for c in meta["review_comments"]:
                    if isinstance(c, dict) and c.get("id") == cid:
                        c["replied"] = True
            flag_modified(db_run, "delivery_metadata")


def process_review_comment_replies(
    session_factory: Any,
    run_info: dict[str, Any],
    repo_spec: str,
    gh_token: str,
) -> None:
    """Process thread replies for completed review-comment repair tasks."""
    if session_factory is None:
        return

    task_id = run_info["task_id"]
    metadata = run_info.get("delivery_metadata") or {}
    pr_number = metadata.get("pr_number")
    if not pr_number:
        return

    replied_ids = set(metadata.get("replied_review_comment_ids") or [])

    # Phase 1: Query DB for unreplied comments and construct reply bodies
    reply_plan = _build_review_comment_reply_plan(session_factory, task_id, replied_ids)
    if not reply_plan:
        return

    # Phase 2 & Phase 3: Execute HTTP calls outside DB session scope, checkpointing DB per success
    for cid, body in reply_plan:
        if reply_to_review_comment(repo_spec, int(pr_number), cid, body, gh_token):
            replied_ids.add(cid)
            _record_review_comment_reply(session_factory, run_info["run_id"], cid, replied_ids)

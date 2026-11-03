"""Helpers for reconciling pending GitHub pull request deliveries."""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from db.models import Task
from orchestrator.github_repo import github_repo_spec_from_url
from sandbox.ephemeral_store_postgres import PostgresEphemeralSecretStore
from sandbox.secrets import SecretExposurePolicy, SecretScope

logger = logging.getLogger(__name__)


def _task_github_token(session: Session, task: Task) -> str | None:
    """Use only the matched task's delivery credentials and configured API token."""
    secrets = task.secrets or {}
    token = secrets.get("GH_TOKEN") or secrets.get("GITHUB_TOKEN")
    if token:
        return token
    store = PostgresEphemeralSecretStore(session)
    for ref in task.secret_refs or []:
        name = ref.get("name")
        if not isinstance(name, str) or not name.startswith("ephem_"):
            continue
        try:
            record = store.get_record(name, task_id=task.id)
        except (RuntimeError, ValueError) as exc:
            logger.info(
                "Unable to resolve task delivery credential",
                extra={"error_type": type(exc).__name__},
            )
            continue
        if (
            record
            and record.required_scope == SecretScope.GIT_PUSH
            and record.exposure_policy == SecretExposurePolicy.BROKER_ONLY
            and record.value
        ):
            return record.value
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def _fetch_current_pull_request(
    repo_url: str, pr_number: int, token: str | None
) -> dict[str, Any] | None:
    """Read the current PR snapshot when a pending webhook cannot order itself."""
    repo_spec = github_repo_spec_from_url(repo_url)
    if not repo_spec or repo_spec.count("/") != 1:
        return None
    owner, repository = repo_spec.split("/", 1)
    request = Request(
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/pulls/{pr_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "code-agent-delivery-reconciliation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed GitHub API host
            snapshot = json.load(response)
    except (json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
        logger.info(
            "Unable to verify current GitHub PR state during pending delivery reconciliation",
            extra={"pr_number": pr_number, "error_type": type(exc).__name__},
        )
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _prepare_pending_pr_payload(
    payload: dict[str, Any],
    repo_url: str,
    pr_number: int | None,
    head_sha: str | None,
    meta: dict[str, Any],
    session: Session,
    task: Task,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """Verify pending head changes when stored timestamps cannot order them."""
    pr = payload.get("pull_request") or {}
    incoming_at, current_at = pr.get("updated_at"), meta.get("pr_updated_at")
    current_head = meta.get("head_sha")
    mismatch = bool(head_sha and current_head and head_sha != current_head)
    unordered = not incoming_at or not current_at or incoming_at == current_at
    if not mismatch or not unordered:
        return payload, False, None

    snapshot = (
        _fetch_current_pull_request(repo_url, pr_number, _task_github_token(session, task))
        if pr_number
        else None
    )
    if snapshot is None:
        logger.info("Pending PR delivery remains retryable until state can be verified")
        return None, False, "pending"
    if (snapshot.get("head") or {}).get("sha") != head_sha:
        logger.info("Ignoring pending PR delivery superseded by current GitHub state")
        return None, False, "ignored"
    return {**payload, "pull_request": snapshot}, True, None


def _known_superseded_pr_event(
    payload: dict[str, Any], event: str, head_sha: str | None, meta: dict[str, Any]
) -> bool:
    """Return whether an ignored pending PR event is known to be stale."""
    if event != "pull_request" or not head_sha or head_sha == meta.get("head_sha"):
        return False
    if head_sha in (meta.get("head_history") or []):
        return True
    incoming = (payload.get("pull_request") or {}).get("updated_at")
    current = meta.get("pr_updated_at")
    return bool(incoming and current and incoming < current)

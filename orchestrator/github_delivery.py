"""Deterministic GitHub draft-PR delivery helpers."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from orchestrator.github_repo import github_repo_spec_from_url
from sandbox.workspace import default_workspace_root

logger = logging.getLogger(__name__)

GITHUB_PR_METADATA_ATTEMPTS = 3
GITHUB_PR_METADATA_RETRY_SECONDS = 2


def capture_delivery_metadata(
    *,
    repo_url: str | None,
    delivery_mode: str,
    branch_name: str,
    token: str | None,
) -> dict[str, Any]:
    """Read allowlisted delivery metadata for an open GitHub pull request."""
    metadata: dict[str, Any] = {
        "delivery_mode": delivery_mode,
        "branch_name": branch_name,
    }
    if delivery_mode != "draft_pr" or not token:
        return metadata

    repo_spec = github_repo_spec_from_url(repo_url)
    if repo_spec is None or repo_spec.count("/") != 1:
        logger.debug("Failed to derive github.com repo spec for delivery metadata")
        return metadata
    owner, repository = repo_spec.split("/", 1)
    query = urlencode({"state": "open", "head": f"{owner}:{branch_name}"})
    request_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/pulls?{query}"
    )
    request = Request(
        request_url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "code-agent-delivery-metadata",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(GITHUB_PR_METADATA_ATTEMPTS):
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API host
                rows = json.load(response)
            if isinstance(rows, list) and rows:
                pull_request = rows[0]
                head = pull_request.get("head") or {}
                return {
                    **metadata,
                    "pr_url": pull_request.get("html_url"),
                    "pr_number": pull_request.get("number"),
                    "head_sha": head.get("sha"),
                }
        except (json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
            logger.debug("Failed to capture PR metadata via GitHub API: %s", exc)
        if attempt + 1 < GITHUB_PR_METADATA_ATTEMPTS:
            time.sleep(GITHUB_PR_METADATA_RETRY_SECONDS)
    return metadata


def _resolve_workspace_path(workspace_id: str) -> Path | None:
    workspace_root = default_workspace_root().resolve()
    workspace_path = (workspace_root / workspace_id).resolve()
    if workspace_root not in workspace_path.parents or not workspace_path.is_dir():
        logger.warning(
            "Draft PR delivery workspace is missing or outside the configured root",
            extra={"workspace_id": workspace_id},
        )
        return None
    return workspace_path


def _push_workspace_head(
    *,
    workspace_path: Path,
    repo_spec: str,
    branch_name: str,
    token: str,
) -> bool:
    encoded_token = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    command_env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded_token}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    remote_url = f"https://github.com/{repo_spec}.git"
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(workspace_path),
                "push",
                remote_url,
                f"HEAD:refs/heads/{branch_name}",
            ],
            capture_output=True,
            check=False,
            env=command_env,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Deterministic draft PR branch push failed",
            extra={"branch_name": branch_name, "error_type": type(exc).__name__},
        )
        return False
    if completed.returncode != 0:
        logger.warning(
            "Deterministic draft PR branch push returned a non-zero status",
            extra={"branch_name": branch_name, "exit_code": completed.returncode},
        )
        return False
    return True


def _create_draft_pr(
    *,
    repo_spec: str,
    branch_name: str,
    base_branch: str,
    pr_title: str,
    pr_body: str,
    token: str,
) -> dict[str, Any] | None:
    owner, repository = repo_spec.split("/", 1)
    request_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}/pulls"
    )
    request = Request(
        request_url,
        data=json.dumps(
            {
                "title": pr_title,
                "head": branch_name,
                "base": base_branch,
                "body": pr_body,
                "draft": True,
            }
        ).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "code-agent-draft-pr-delivery",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API host
            pull_request = json.load(response)
    except HTTPError as exc:
        logger.warning(
            "GitHub rejected deterministic draft PR creation",
            extra={"branch_name": branch_name, "status_code": exc.code},
        )
        return None
    except (json.JSONDecodeError, OSError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Deterministic draft PR creation failed",
            extra={"branch_name": branch_name, "error_type": type(exc).__name__},
        )
        return None

    if not isinstance(pull_request, dict) or not pull_request.get("html_url"):
        logger.warning(
            "GitHub draft PR creation returned incomplete metadata",
            extra={"branch_name": branch_name},
        )
        return None
    head = pull_request.get("head") or {}
    return {
        "delivery_mode": "draft_pr",
        "branch_name": branch_name,
        "pr_url": pull_request.get("html_url"),
        "pr_number": pull_request.get("number"),
        "head_sha": head.get("sha"),
    }


def publish_draft_pr_from_workspace(
    *,
    repo_url: str | None,
    workspace_id: str,
    branch_name: str,
    base_branch: str,
    pr_title: str,
    pr_body: str,
    token: str,
) -> dict[str, Any] | None:
    """Push the retained workspace HEAD and create a confirmed GitHub draft PR."""
    repo_spec = github_repo_spec_from_url(repo_url)
    if repo_spec is None or repo_spec.count("/") != 1:
        logger.warning("Draft PR delivery requires a github.com repository URL")
        return None
    workspace_path = _resolve_workspace_path(workspace_id)
    if workspace_path is None:
        return None
    if not _push_workspace_head(
        workspace_path=workspace_path,
        repo_spec=repo_spec,
        branch_name=branch_name,
        token=token,
    ):
        return None
    return _create_draft_pr(
        repo_spec=repo_spec,
        branch_name=branch_name,
        base_branch=base_branch,
        pr_title=pr_title,
        pr_body=pr_body,
        token=token,
    )

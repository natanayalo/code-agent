"""GitHub repository identifier helpers."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def github_repo_spec_from_url(repo_url: str | None) -> str | None:
    """Return the gh CLI -R owner/repo value for a GitHub clone URL."""
    if not repo_url:
        return None

    stripped = repo_url.strip()
    if not stripped:
        return None

    ssh_match = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", stripped)
    if ssh_match:
        host, owner, repo = ssh_match.groups()
        if host == "github.com":
            return f"{owner}/{repo}"
        return f"{host}/{owner}/{repo}"

    parsed = urlparse(stripped)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.netloc:
        path_parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(path_parts) >= 2:
            owner, repo = path_parts[:2]
            if repo.endswith(".git"):
                repo = repo[:-4]
            if parsed.netloc == "github.com":
                return f"{owner}/{repo}"
            return f"{parsed.netloc}/{owner}/{repo}"

    owner_repo_match = re.match(r"(?:([^/\s]+)/)?([^/\s]+)/([^/\s]+)$", stripped)
    if owner_repo_match:
        host, owner, repo = owner_repo_match.groups()
        if repo.endswith(".git"):
            repo = repo[:-4]
        if host == "github.com":
            return f"{owner}/{repo}"
        return f"{host}/{owner}/{repo}" if host else f"{owner}/{repo}"

    return None

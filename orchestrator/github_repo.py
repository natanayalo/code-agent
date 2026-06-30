"""GitHub repository identifier helpers."""

from __future__ import annotations

from urllib.parse import urlparse


def github_repo_spec_from_url(repo_url: str | None) -> str | None:
    """Return the gh CLI -R owner/repo value for a GitHub clone URL."""
    if not repo_url:
        return None

    stripped = repo_url.strip()
    if not stripped:
        return None

    if stripped.startswith("git@"):
        stripped = "ssh://" + stripped.replace(":", "/", 1)
    elif not stripped.startswith(("http://", "https://", "ssh://", "git://")):
        parts = stripped.split("/")
        if len(parts) == 2:
            stripped = "https://github.com/" + stripped
        else:
            stripped = "https://" + stripped

    parsed = urlparse(stripped)
    path_parts = [part for part in parsed.path.strip("/").split("/") if part]

    if len(path_parts) >= 2:
        owner, repo = path_parts[:2]
        if repo.endswith(".git"):
            repo = repo[:-4]

        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.split("@")[-1]

        if netloc == "github.com":
            return f"{owner}/{repo}"
        elif netloc:
            return f"{netloc}/{owner}/{repo}"
        else:
            return f"{owner}/{repo}"

    return None

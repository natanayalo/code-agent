"""Shared configuration for API system-wide settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from apps.runtime import _is_enabled, coerce_non_negative_int_env
from sandbox.container import DEFAULT_SANDBOX_IMAGE
from sandbox.workspace import default_workspace_root

DEFAULT_SCOUT_TASK_TEXT = (
    "Scout this repository for small, low-risk improvement proposals. "
    "Do not modify files. Produce concise findings with evidence, "
    "expected impact, and suggested verification."
)


def _parse_repo_map(repo_str: str) -> dict[str, str]:
    """Parse comma-separated key:url pairs into a repository registry."""
    repos: dict[str, str] = {}
    if not repo_str:
        return repos
    for pair in repo_str.split(","):
        if ":" not in pair:
            continue
        key, url = pair.split(":", 1)
        key = key.strip()
        url = url.strip()
        if key and url:
            repos[key] = url
    return repos


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Consolidated system-level configuration."""

    default_image: str
    workspace_root: str
    scout_scheduler_enabled: bool = False
    scout_idle_trigger_minutes: int = 30
    scout_schedule_interval_minutes: int = 1440
    scout_task_text: str = DEFAULT_SCOUT_TASK_TEXT
    scout_repo_key: str | None = None
    scout_branch: str | None = None
    allowed_repos: dict[str, str] = field(default_factory=dict)
    scout_allowed_repos: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load_from_env(cls, env: Mapping[str, str] | None = None) -> SystemConfig:
        """Load and normalize system configuration from environment variables."""
        environ = env if env is not None else os.environ
        image = environ.get("CODE_AGENT_SANDBOX_IMAGE", "").strip() or DEFAULT_SANDBOX_IMAGE
        workspace_root = environ.get("CODE_AGENT_WORKSPACE_ROOT", "").strip() or str(
            default_workspace_root(environ)
        )

        allowed_repos_str = environ.get("CODE_AGENT_ALLOWED_REPOS", "").strip()
        scout_allowed_repos_str = environ.get("CODE_AGENT_SCOUT_ALLOWED_REPOS", "").strip()

        allowed_repos = _parse_repo_map(allowed_repos_str)
        scout_allowed_repos = _parse_repo_map(scout_allowed_repos_str)
        for key, url in scout_allowed_repos.items():
            allowed_repos.setdefault(key, url)

        return cls(
            default_image=image,
            workspace_root=workspace_root,
            scout_scheduler_enabled=_is_enabled(
                environ.get("CODE_AGENT_SCOUT_SCHEDULER_ENABLED"), default=False
            ),
            scout_idle_trigger_minutes=coerce_non_negative_int_env(
                environ.get("CODE_AGENT_SCOUT_IDLE_MINUTES"), default=30
            ),
            scout_schedule_interval_minutes=coerce_non_negative_int_env(
                environ.get("CODE_AGENT_SCOUT_SCHEDULE_INTERVAL_MINUTES"), default=1440
            ),
            scout_task_text=environ.get("CODE_AGENT_SCOUT_TASK_TEXT", "").strip()
            or DEFAULT_SCOUT_TASK_TEXT,
            scout_repo_key=(
                environ.get("CODE_AGENT_SCOUT_REPO_KEY")
                or environ.get("CODE_AGENT_SCOUT_REPO_URL")
                or ""
            ).strip()
            or None,
            scout_branch=(environ.get("CODE_AGENT_SCOUT_BRANCH") or "").strip() or None,
            allowed_repos=allowed_repos,
            scout_allowed_repos=scout_allowed_repos,
        )

    def resolve_repo_key(self, key: str | None) -> str | None:
        """Resolve a repo key to a URL using the allowed_repos registry."""
        if not key:
            return None
        return self.allowed_repos.get(key)

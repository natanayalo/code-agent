"""Repo-level validation profiles and setup commands."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sandbox.policy import LocalRepoPolicyError, validate_local_repo_path

logger = logging.getLogger(__name__)


class InvalidRepoProfileError(Exception):
    """Raised when a repo profile exists but fails schema validation or YAML parsing."""

    pass


class SetupProfile(BaseModel):
    """Configuration for setting up the environment before tasks."""

    commands: list[str] = Field(default_factory=list)


class ValidationProfile(BaseModel):
    """Validation tiers for different risk levels."""

    quick: list[str] = Field(default_factory=list)
    full: list[str] = Field(default_factory=list)


class DeliveryProfile(BaseModel):
    """Delivery configuration defaults."""

    default_mode: Literal["summary", "workspace", "branch", "draft_pr"] = "workspace"


class RepoProfile(BaseModel):
    """Root configuration loaded from code-agent.project.yaml."""

    model_config = ConfigDict(extra="ignore")

    setup: SetupProfile = Field(default_factory=SetupProfile)
    validation: ValidationProfile = Field(default_factory=ValidationProfile)
    protected_paths: list[str] = Field(default_factory=list)
    approval_required: list[str] = Field(default_factory=list)
    delivery: DeliveryProfile = Field(default_factory=DeliveryProfile)


def _local_repo_path_from_file_url(repo_url: str) -> str | None:
    parsed_url = urlparse(repo_url)
    if parsed_url.scheme != "file":
        return None
    if not parsed_url.path:
        return None
    return os.path.abspath(url2pathname(parsed_url.path))


def load_repo_profile(repo_url: str | None) -> RepoProfile | None:
    """Load and validate code-agent.project.yaml from a local repository."""
    if not repo_url:
        return None

    local_repo_path = _local_repo_path_from_file_url(repo_url)
    if not local_repo_path:
        logger.debug("Skipping remote repo profile loading (only local supported in v1).")
        return None

    try:
        validate_local_repo_path(local_repo_path)
    except LocalRepoPolicyError as exc:
        logger.warning(f"Skipping repo profile loading due to policy violation: {exc}")
        return None

    profile_path = Path(local_repo_path) / "code-agent.project.yaml"
    if not profile_path.is_file():
        return None

    try:
        content = profile_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        if parsed is None:
            parsed = {}
        elif not isinstance(parsed, dict):
            raise InvalidRepoProfileError(f"Invalid format in {profile_path}: expected dictionary")
        return RepoProfile.model_validate(parsed)
    except yaml.YAMLError as exc:
        raise InvalidRepoProfileError(f"Failed to parse YAML in {profile_path}: {exc}") from exc
    except ValidationError as exc:
        raise InvalidRepoProfileError(f"Invalid schema in {profile_path}: {exc}") from exc
    except OSError as exc:
        logger.warning(f"Failed to read {profile_path}: {exc}")

    return None

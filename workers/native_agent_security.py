"""Security grants shared by native-provider worker adapters."""

from __future__ import annotations

from tools import EXECUTE_GITHUB_TOOL_NAME, ToolPermissionLevel, granted_permission_from_constraints
from workers.base import WorkerRequest


def native_github_credentials(request: WorkerRequest) -> dict[str, str]:
    """Return GitHub credentials only for an explicit privileged GitHub grant."""
    tools = request.tools or []
    permission = granted_permission_from_constraints(request.constraints)
    if EXECUTE_GITHUB_TOOL_NAME not in tools or permission not in {
        ToolPermissionLevel.NETWORKED_WRITE,
        ToolPermissionLevel.GIT_PUSH_OR_DEPLOY,
    }:
        return {}
    return {
        key: value
        for key, value in request.secrets.items()
        if key in {"GH_TOKEN", "GITHUB_TOKEN"} and value
    }

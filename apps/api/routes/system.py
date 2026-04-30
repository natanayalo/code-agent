"""System configuration routes for the code-agent service."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from apps.api.dependencies import require_any_valid_auth
from sandbox.container import DEFAULT_SANDBOX_IMAGE
from sandbox.workspace import default_workspace_root
from tools.registry import DEFAULT_TOOL_REGISTRY, ToolDefinition

router = APIRouter(
    prefix="/system", tags=["system"], dependencies=[Depends(require_any_valid_auth)]
)


class SandboxStatusResponse(BaseModel):
    """Configuration and status of the task sandbox."""

    default_image: str
    workspace_root: str


@router.get(
    "/tools", response_model=list[ToolDefinition], response_model_exclude={"mcp_input_schema"}
)
def list_tools() -> list[ToolDefinition]:
    """Return the registry of tools available to the worker runtime."""
    return list(DEFAULT_TOOL_REGISTRY.list_tools())


@router.get("/sandbox", response_model=SandboxStatusResponse)
def get_sandbox_status() -> SandboxStatusResponse:
    """Return the configuration and status of the task sandbox."""
    image = os.environ.get("CODE_AGENT_SANDBOX_IMAGE", "").strip() or DEFAULT_SANDBOX_IMAGE
    workspace_root = str(default_workspace_root())
    return SandboxStatusResponse(
        default_image=image,
        workspace_root=workspace_root,
    )

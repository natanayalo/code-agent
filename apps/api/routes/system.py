"""System configuration routes for the code-agent service."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from apps.api.config import SystemConfig
from apps.api.dependencies import get_system_config, require_any_valid_auth
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
def get_sandbox_status(
    config: SystemConfig = Depends(get_system_config),
) -> SandboxStatusResponse:
    """Return the configuration and status of the task sandbox."""
    return SandboxStatusResponse(
        default_image=config.default_image,
        workspace_root=config.workspace_root,
    )

"""System configuration routes for the code-agent service."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from apps.api.config import SystemConfig
from apps.api.dependencies import get_system_config, require_any_valid_auth
from orchestrator.provider_diagnostics import ProviderDiagnosticsService
from orchestrator.provider_diagnostics_types import SystemProviderDiagnosticsReport
from orchestrator.runtime_manifest import RuntimeManifest, build_runtime_manifest
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


@router.get("/runtime-manifest", response_model=RuntimeManifest)
def get_runtime_manifest(
    config: SystemConfig = Depends(get_system_config),
) -> RuntimeManifest:
    """Return the baseline runtime operating contract for operators."""
    return build_runtime_manifest(
        default_image=config.default_image,
        workspace_root=config.workspace_root,
    )


@router.get("/provider-diagnostics", response_model=SystemProviderDiagnosticsReport)
async def get_provider_diagnostics(
    request: Request,
    required_providers: list[str] | None = Query(default=None),
) -> SystemProviderDiagnosticsReport:
    """Return provider readiness diagnostics for this API process host."""
    task_service = getattr(request.app.state, "task_service", None)
    service = ProviderDiagnosticsService(
        worker=getattr(task_service, "worker", None),
        secret_registry=getattr(task_service, "secret_registry", None),
    )
    req_set = set(required_providers) if required_providers else None
    return await service.evaluate_all_providers(
        required_providers=req_set,
        target="api_process",
    )

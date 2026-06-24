"""Runtime operating contract models and builders."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, get_args

from pydantic import BaseModel, ConfigDict, Field

from sandbox.container import DEFAULT_SANDBOX_IMAGE
from sandbox.workspace import default_workspace_root
from tools.registry import DEFAULT_TOOL_REGISTRY, ToolDefinition, ToolRegistry
from workers.base import MaintenanceActionType


class RuntimeManifestModel(BaseModel):
    """Base model for runtime manifest contracts."""

    model_config = ConfigDict(extra="forbid")


class MaintenanceActionDefinition(RuntimeManifestModel):
    """A maintenance action workers may request but never execute directly."""

    action: MaintenanceActionType
    description: str
    request_only: bool = True
    requires_operator_approval: bool = True


class RuntimeServiceIdentity(RuntimeManifestModel):
    """Identity fields for the running code-agent service."""

    service_name: str = "code-agent"
    schema_version: int = 1
    environment: str = "local"
    build_sha: str | None = None


class RuntimeSandboxManifest(RuntimeManifestModel):
    """Sandbox configuration visible to workers and operators."""

    default_image: str
    workspace_root: str


class RuntimeWorkerManifest(RuntimeManifestModel):
    """Selected worker/runtime identity for a task execution."""

    worker_type: str | None = None
    worker_profile: str | None = None
    runtime_mode: str | None = None
    workspace_id: str | None = None


class RuntimeTaskPolicyManifest(RuntimeManifestModel):
    """Task-level operating constraints for the worker."""

    read_only: bool = False
    network_enabled: bool = False
    delivery_mode: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    approval_required: bool = False


class RuntimeToolManifest(RuntimeManifestModel):
    """Compact tool capability declaration for the runtime manifest."""

    name: str
    capability_category: str
    side_effect_level: str
    required_permission: str
    network_required: bool
    deterministic: bool


class RuntimeManifest(RuntimeManifestModel):
    """Versioned operating contract exposed to workers and operators."""

    service: RuntimeServiceIdentity = Field(default_factory=RuntimeServiceIdentity)
    sandbox: RuntimeSandboxManifest
    worker: RuntimeWorkerManifest = Field(default_factory=RuntimeWorkerManifest)
    task: RuntimeTaskPolicyManifest = Field(default_factory=RuntimeTaskPolicyManifest)
    tools: list[RuntimeToolManifest] = Field(default_factory=list)
    approval_capabilities: list[str] = Field(
        default_factory=lambda: ["clarification", "permission", "manual_approval"]
    )
    maintenance_actions: list[MaintenanceActionDefinition] = Field(default_factory=list)


_MAINTENANCE_ACTION_DESCRIPTIONS: Final[dict[MaintenanceActionType, str]] = {
    "restart_worker": "Request that an operator restarts a stuck or unhealthy queue worker.",
    "recycle_sandbox": "Request disposal and recreation of the current sandbox workspace.",
    "reload_config": "Request that runtime configuration is reloaded by the operator.",
    "dependency_refresh": (
        "Request dependency installation or cache refresh outside normal task flow."
    ),
    "operator_attention": (
        "Request direct operator attention for an ambiguous or blocked condition."
    ),
}

_MAINTENANCE_ACTIONS: Final[tuple[MaintenanceActionDefinition, ...]] = tuple(
    MaintenanceActionDefinition(
        action=action,
        description=_MAINTENANCE_ACTION_DESCRIPTIONS[action],
    )
    for action in get_args(MaintenanceActionType)
)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw)


def _mapping_or_model_value(source: Mapping[str, Any] | object | None, key: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | Mapping):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _tool_manifest(tool: ToolDefinition) -> RuntimeToolManifest:
    return RuntimeToolManifest(
        name=tool.name,
        capability_category=_enum_value(tool.capability_category) or "",
        side_effect_level=_enum_value(tool.side_effect_level) or "",
        required_permission=_enum_value(tool.required_permission) or "",
        network_required=tool.network_required,
        deterministic=tool.deterministic,
    )


def _selected_tools(
    registry: ToolRegistry,
    requested_tools: Sequence[str] | None,
) -> list[RuntimeToolManifest]:
    if requested_tools is None:
        return [_tool_manifest(tool) for tool in registry.list_tools()]
    manifests: list[RuntimeToolManifest] = []
    for name in requested_tools:
        tool = registry.get_tool(name)
        if tool is not None:
            manifests.append(_tool_manifest(tool))
    return manifests


def build_runtime_manifest(
    *,
    default_image: str | None = None,
    workspace_root: str | None = None,
    environment: str | None = None,
    build_sha: str | None = None,
    worker_type: Any | None = None,
    worker_profile: str | None = None,
    runtime_mode: Any | None = None,
    workspace_id: str | None = None,
    task_spec: Mapping[str, Any] | object | None = None,
    read_only: bool = False,
    network_enabled: bool = False,
    budget: Mapping[str, Any] | None = None,
    requested_tools: Sequence[str] | None = None,
    tool_registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
) -> RuntimeManifest:
    """Build a runtime operating contract from existing execution context."""

    resolved_environment = (
        environment or os.getenv("APP_ENV") or os.getenv("CODE_AGENT_ENV") or "local"
    )
    resolved_build_sha = build_sha or os.getenv("BUILD_SHA") or os.getenv("COMMIT_SHA")
    approval_required = bool(_mapping_or_model_value(task_spec, "requires_permission"))
    return RuntimeManifest(
        service=RuntimeServiceIdentity(
            environment=resolved_environment, build_sha=resolved_build_sha
        ),
        sandbox=RuntimeSandboxManifest(
            default_image=default_image or DEFAULT_SANDBOX_IMAGE,
            workspace_root=workspace_root or str(default_workspace_root()),
        ),
        worker=RuntimeWorkerManifest(
            worker_type=_enum_value(worker_type),
            worker_profile=worker_profile,
            runtime_mode=_enum_value(runtime_mode),
            workspace_id=workspace_id,
        ),
        task=RuntimeTaskPolicyManifest(
            read_only=read_only,
            network_enabled=network_enabled,
            delivery_mode=_enum_value(_mapping_or_model_value(task_spec, "delivery_mode")),
            budget=dict(budget or {}),
            allowed_actions=_string_list(_mapping_or_model_value(task_spec, "allowed_actions")),
            forbidden_actions=_string_list(_mapping_or_model_value(task_spec, "forbidden_actions")),
            approval_required=approval_required,
        ),
        tools=_selected_tools(tool_registry, requested_tools),
        maintenance_actions=list(_MAINTENANCE_ACTIONS),
    )

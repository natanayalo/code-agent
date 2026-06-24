"""Unit tests for the shared worker contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workers import (
    SUPPORTED_WORKER_TYPES,
    MaintenanceRequest,
    WorkerProfile,
    WorkerRequest,
    WorkerResult,
)


def test_worker_request_supports_contract_fields() -> None:
    """Worker request models accept the documented contract fields."""
    request = WorkerRequest(
        session_id="session-1",
        repo_url="https://github.com/natanayalo/code-agent",
        branch="task/t-040-worker-interface",
        task_text="Define worker interface",
        memory_context={"project": [{"memory_key": "pitfall"}]},
        task_spec={"goal": "Define worker interface", "risk_level": "low"},
        constraints={"requires_approval": False},
        budget={"max_minutes": 15},
        worker_profile="codex-native-executor",
        runtime_mode="native_agent",
        runtime_manifest={
            "service": {"service_name": "code-agent", "schema_version": 1},
            "task": {"read_only": False},
        },
    )

    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.task_text == "Define worker interface"
    assert request.task_spec == {"goal": "Define worker interface", "risk_level": "low"}
    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"
    assert request.runtime_manifest is not None
    assert request.runtime_manifest["task"] == {"read_only": False}


def test_worker_request_rejects_unknown_fields() -> None:
    """Worker request models stay inspectable by rejecting extra data."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkerRequest(task_text="Define worker interface", unexpected="value")


def test_worker_result_requires_known_status_values() -> None:
    """Worker result models reject unsupported status strings."""
    with pytest.raises(ValidationError, match="Input should be"):
        WorkerResult(status="unknown")


def test_worker_result_non_success_defaults_failure_kind() -> None:
    """Failure outcomes should always carry an explicit failure taxonomy value."""
    result = WorkerResult(status="failure", summary="something failed")
    assert result.failure_kind == "unknown"


def test_worker_result_supports_request_only_maintenance_signals() -> None:
    """Workers may request maintenance without gaining execution authority."""
    result = WorkerResult(
        status="failure",
        summary="sandbox stopped responding",
        maintenance_requests=[
            MaintenanceRequest(
                action="recycle_sandbox",
                reason="Command execution stopped producing output.",
                evidence=["heartbeat expired"],
                scope="sandbox",
            )
        ],
    )

    assert result.maintenance_requests[0].action == "recycle_sandbox"
    assert result.maintenance_requests[0].scope == "sandbox"
    assert result.maintenance_requests[0].risk == "medium"


def test_maintenance_request_rejects_unknown_actions() -> None:
    """Maintenance request action vocabulary should stay explicit."""
    with pytest.raises(ValidationError, match="Input should be"):
        MaintenanceRequest(action="deploy_prod", reason="try it")  # type: ignore[arg-type]


def test_worker_profile_supports_runtime_shapes() -> None:
    """Profiles validate codex/antigravity/openrouter plus planner/reviewer runtime modes."""
    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    )
    antigravity_planner_profile = WorkerProfile(
        name="antigravity-planner",
        worker_type="antigravity",
        runtime_mode="planner_only",
        capability_tags=["planning"],
        mutation_policy="read_only",
        self_review_policy="never",
        supported_delivery_modes=["summary"],
    )
    antigravity_reviewer_profile = WorkerProfile(
        name="antigravity-reviewer",
        worker_type="antigravity",
        runtime_mode="reviewer_only",
        capability_tags=["review"],
        mutation_policy="read_only",
        self_review_policy="always",
        supported_delivery_modes=["summary"],
    )
    openrouter_profile = WorkerProfile(
        name="openrouter-tool-loop",
        worker_type="openrouter",
        runtime_mode="tool_loop",
        capability_tags=["execution"],
        permission_profile="workspace_write",
        supported_delivery_modes=["workspace"],
    )

    assert codex_profile.runtime_mode == "native_agent"
    assert antigravity_planner_profile.runtime_mode == "planner_only"
    assert antigravity_reviewer_profile.runtime_mode == "reviewer_only"
    assert openrouter_profile.runtime_mode == "tool_loop"


def test_worker_profile_coerces_retired_gemini_worker_type() -> None:
    """Gemini is coerced to Antigravity for backward compatibility."""

    profile = WorkerProfile(
        name="test",
        worker_type="gemini",  # type: ignore
        runtime_mode="native_agent",
    )
    assert profile.worker_type == "antigravity"


def test_worker_profile_rejects_unknown_runtime_modes() -> None:
    """Runtime mode vocabulary should remain explicit and typed."""
    with pytest.raises(ValidationError, match="Input should be"):
        WorkerProfile(
            name="bad-profile",
            worker_type="codex",
            runtime_mode="interactive",
        )


def test_worker_profile_normalizes_duplicate_list_entries() -> None:
    """Profile lists should normalize once at validation time."""
    profile = WorkerProfile(
        name="normalized-profile",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution", "planning", "execution"],
        supported_delivery_modes=["workspace", "draft_pr", "workspace"],
    )

    assert profile.capability_tags == ["execution", "planning"]
    assert profile.supported_delivery_modes == ["draft_pr", "workspace"]


def test_worker_profile_invalid_unhashable_list_entry_raises_validation_error() -> None:
    """Unhashable items should not crash list normalization with a TypeError."""
    with pytest.raises(ValidationError, match="Input should be"):
        WorkerProfile(
            name="bad-tags",
            worker_type="codex",
            runtime_mode="native_agent",
            capability_tags=[{"unexpected": "dict"}],
        )


def test_supported_worker_types_contract_order() -> None:
    """Fallback order should be declared in one shared contract constant."""
    assert SUPPORTED_WORKER_TYPES == ("antigravity", "openrouter", "codex")

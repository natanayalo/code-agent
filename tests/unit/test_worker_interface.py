"""Unit tests for the shared worker contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workers import WorkerProfile, WorkerRequest, WorkerResult


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
    )

    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.task_text == "Define worker interface"
    assert request.task_spec == {"goal": "Define worker interface", "risk_level": "low"}
    assert request.worker_profile == "codex-native-executor"
    assert request.runtime_mode == "native_agent"


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


def test_worker_profile_supports_milestone_17_runtime_shapes() -> None:
    """Profiles validate codex/gemini/openrouter plus planner/reviewer runtime modes."""
    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
        capability_tags=["execution"],
        supported_delivery_modes=["workspace", "branch", "draft_pr"],
    )
    gemini_planner_profile = WorkerProfile(
        name="gemini-planner",
        worker_type="gemini",
        runtime_mode="planner_only",
        capability_tags=["planning"],
        mutation_policy="read_only",
        self_review_policy="never",
        supported_delivery_modes=["summary"],
    )
    gemini_reviewer_profile = WorkerProfile(
        name="gemini-reviewer",
        worker_type="gemini",
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
    assert gemini_planner_profile.runtime_mode == "planner_only"
    assert gemini_reviewer_profile.runtime_mode == "reviewer_only"
    assert openrouter_profile.runtime_mode == "tool_loop"


def test_worker_profile_rejects_unknown_runtime_modes() -> None:
    """Runtime mode vocabulary should remain explicit and typed."""
    with pytest.raises(ValidationError, match="Input should be"):
        WorkerProfile(
            name="bad-profile",
            worker_type="codex",
            runtime_mode="interactive",
        )

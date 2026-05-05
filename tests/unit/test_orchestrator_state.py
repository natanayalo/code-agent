"""Unit tests for orchestrator state models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator import OrchestratorState


def test_orchestrator_state_supports_minimal_task_input() -> None:
    """A task-only payload can initialize the workflow state with safe defaults."""
    state = OrchestratorState(task={"task_text": "Add webhook ingress"})

    assert state.current_step == "ingest_task"
    assert state.task.task_text == "Add webhook ingress"
    assert state.task.priority == 0
    assert state.memory.personal == []
    assert state.route.chosen_worker is None
    assert state.task_spec is None
    assert state.approval.required is False
    assert state.approval.status == "not_required"
    assert state.progress_updates == []
    assert state.errors == []


def test_orchestrator_state_preserves_secret_whitespace() -> None:
    """Secret values should remain byte-exact and must not be globally trimmed."""
    state = OrchestratorState(
        task={
            "task_text": "Run with exact secret",
            "secrets": {
                "TOKEN": "  leading-and-trailing  ",
                "MIXED": "a b",
            },
        }
    )

    assert state.task.secrets["TOKEN"] == "  leading-and-trailing  "
    assert state.task.secrets["MIXED"] == "a b"


def test_orchestrator_state_supports_nested_workflow_data() -> None:
    """Nested workflow fields are validated and preserved in the typed state."""
    state = OrchestratorState(
        current_step="await_result",
        session={
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "telegram",
            "external_thread_id": "thread-1",
        },
        task={
            "task_id": "task-1",
            "task_text": "Route to codex",
            "repo_url": "https://github.com/natanayalo/code-agent",
            "branch": "master",
            "worker_override": "codex",
            "worker_profile_override": "codex-native-executor",
        },
        task_spec={
            "goal": "Route to codex",
            "repo_url": "https://github.com/natanayalo/code-agent",
            "target_branch": "master",
            "risk_level": "low",
            "task_type": "feature",
            "delivery_mode": "workspace",
        },
        memory={
            "personal": [
                {
                    "memory_key": "communication_preferences",
                    "value": {"style": "direct"},
                }
            ],
            "project": [
                {
                    "memory_key": "known_pitfalls",
                    "value": {"docker": "use cert.pem when needed"},
                }
            ],
            "session": {"last_worker": "codex"},
        },
        route={
            "chosen_worker": "codex",
            "chosen_profile": "codex-native-executor",
            "runtime_mode": "native_agent",
            "route_reason": "manual_override",
            "override_applied": True,
        },
        approval={
            "required": True,
            "status": "approved",
            "approval_type": "destructive_action",
            "reason": "Deletes files from the workspace",
            "resume_token": "approval-task-1",
        },
        dispatch={
            "run_id": "run-1",
            "worker_type": "codex",
            "worker_profile": "codex-native-executor",
            "runtime_mode": "native_agent",
            "workspace_id": "workspace-1",
        },
        result={
            "status": "success",
            "summary": "Repository layer added",
            "commands_run": [{"command": "pytest", "exit_code": 0, "duration_seconds": 3.2}],
            "files_changed": ["repositories/sqlalchemy.py"],
            "test_results": [{"name": "pytest", "status": "passed"}],
            "artifacts": [
                {
                    "name": "stdout.log",
                    "uri": "artifacts/stdout.log",
                    "artifact_type": "log",
                }
            ],
            "next_action_hint": "persist_memory",
        },
        memory_to_persist=[
            {
                "category": "project",
                "memory_key": "successful_command",
                "value": {"command": "pytest tests/integration/test_repositories.py"},
                "repo_url": "https://github.com/natanayalo/code-agent",
            }
        ],
        progress_updates=["task accepted", "worker dispatched"],
        attempt_count=1,
    )

    assert state.session is not None
    assert state.session.channel == "telegram"
    assert state.route.chosen_worker == "codex"
    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.runtime_mode == "native_agent"
    assert state.approval.status == "approved"
    assert state.result is not None
    assert state.result.commands_run[0].command == "pytest"
    assert state.task_spec is not None
    assert state.task_spec.goal == "Route to codex"
    assert state.memory.project[0].memory_key == "known_pitfalls"
    assert state.memory_to_persist[0].category == "project"


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"task": {"task_text": "Bad priority", "priority": -1}}, "greater than or equal to 0"),
        ({"task": {"task_text": "Bad worker", "worker_override": "unknown"}}, "Input should be"),
        ({"task": {"task_text": "Bad step"}, "current_step": "unknown"}, "Input should be"),
        (
            {"task": {"task_text": "Extra data"}, "unexpected": "field"},
            "Extra inputs are not permitted",
        ),
    ],
)
def test_orchestrator_state_rejects_invalid_payloads(
    payload: dict[str, object],
    expected_fragment: str,
) -> None:
    """Invalid values fail validation instead of leaking into workflow state."""
    with pytest.raises(ValidationError, match=expected_fragment):
        OrchestratorState(**payload)

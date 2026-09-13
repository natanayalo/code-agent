from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from workers.agent_event import (
    AgentCompleted,
    AgentEvent,
    AgentFailed,
    AgentMessage,
    AgentProgress,
    AgentStarted,
    ArtifactProduced,
    BudgetUpdated,
    FileChanged,
    PermissionRequested,
    ToolCompleted,
    ToolRequested,
    assert_event_sequence_monotonic,
)
from workers.constants import (
    AGENT_EVENT_MAX_MESSAGE_CHARS,
    AGENT_EVENT_MAX_SUMMARY_CHARS,
    AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS,
)


def _ts(sec: int = 0) -> datetime:
    return datetime(2026, 9, 12, 12, 0, sec, tzinfo=UTC)


def test_agent_started_variant():
    event = AgentStarted(
        run_id="run_1",
        sequence=1,
        timestamp=_ts(1),
        worker_type="codex",
        command="codex exec ...",
    )
    assert event.event_type == "agent_started"
    assert event.sequence == 1
    assert event.worker_type == "codex"
    assert event.command == "codex exec ..."


def test_agent_progress_variant():
    event = AgentProgress(
        run_id="run_1",
        sequence=2,
        timestamp=_ts(2),
        phase="planning",
        message="Reading files",
    )
    assert event.event_type == "agent_progress"
    assert event.phase == "planning"
    assert event.message == "Reading files"


def test_agent_message_variant():
    event = AgentMessage(
        run_id="run_1",
        sequence=3,
        timestamp=_ts(3),
        role="assistant",
        content="I have updated the code.",
    )
    assert event.event_type == "agent_message"
    assert event.role == "assistant"
    assert event.content == "I have updated the code."


def test_tool_requested_variant():
    event = ToolRequested(
        run_id="run_1",
        sequence=4,
        timestamp=_ts(4),
        call_id="call_1",
        tool_name="edit_file",
        input_summary='{"path": "test.py"}',
    )
    assert event.event_type == "tool_requested"
    assert event.call_id == "call_1"
    assert event.tool_name == "edit_file"


def test_tool_completed_variant():
    event = ToolCompleted(
        run_id="run_1",
        sequence=5,
        timestamp=_ts(5),
        call_id="call_1",
        tool_name="edit_file",
        exit_code=0,
        output_summary='{"status": "ok"}',
        duration_seconds=1.2,
    )
    assert event.event_type == "tool_completed"
    assert event.exit_code == 0
    assert event.duration_seconds == 1.2


def test_file_changed_variant():
    event = FileChanged(
        run_id="run_1",
        sequence=6,
        timestamp=_ts(6),
        path="tests/unit/test_app.py",
        change_kind="modified",
    )
    assert event.event_type == "file_changed"
    assert event.change_kind == "modified"
    assert event.path == "tests/unit/test_app.py"


def test_permission_requested_variant():
    event = PermissionRequested(
        run_id="run_1",
        sequence=7,
        timestamp=_ts(7),
        permission_type="execute_command",
        reason="Run pytest",
        granted=True,
    )
    assert event.event_type == "permission_requested"
    assert event.permission_type == "execute_command"
    assert event.granted is True


def test_artifact_produced_variant():
    event = ArtifactProduced(
        run_id="run_1",
        sequence=8,
        timestamp=_ts(8),
        name="test_patch",
        uri="patches/01.patch",
        artifact_type="patch",
    )
    assert event.event_type == "artifact_produced"
    assert event.name == "test_patch"
    assert event.artifact_type == "patch"


def test_budget_updated_variant():
    event = BudgetUpdated(
        run_id="run_1",
        sequence=9,
        timestamp=_ts(9),
        tokens_used=1500,
        cost_usd=0.03,
        context_window_ratio=0.45,
    )
    assert event.event_type == "budget_updated"
    assert event.tokens_used == 1500
    assert event.cost_usd == 0.03
    assert event.context_window_ratio == 0.45


def test_agent_failed_variant():
    event = AgentFailed(
        run_id="run_1",
        sequence=10,
        timestamp=_ts(10),
        failure_kind="rate_limit",
        failure_summary="Too many requests",
        exit_code=1,
    )
    assert event.event_type == "agent_failed"
    assert event.failure_kind == "rate_limit"
    assert event.failure_summary == "Too many requests"


def test_agent_completed_variant():
    event = AgentCompleted(
        run_id="run_1",
        sequence=11,
        timestamp=_ts(11),
        final_summary="All tasks complete",
        exit_code=0,
    )
    assert event.event_type == "agent_completed"
    assert event.final_summary == "All tasks complete"


def test_agent_event_extra_forbidden():
    with pytest.raises(ValidationError):
        AgentProgress(
            run_id="run_1",
            sequence=1,
            timestamp=_ts(1),
            phase="test",
            extra_field="disallowed",
        )


def test_agent_event_round_trip_serialization():
    adapter = TypeAdapter(AgentEvent)
    events: list[AgentEvent] = [
        AgentStarted(
            run_id="run_1",
            sequence=1,
            timestamp=_ts(1),
            worker_type="antigravity",
        ),
        AgentProgress(
            run_id="run_1",
            sequence=2,
            timestamp=_ts(2),
            phase="executing",
        ),
        AgentCompleted(
            run_id="run_1",
            sequence=3,
            timestamp=_ts(3),
            final_summary="Success",
        ),
    ]

    for orig in events:
        serialized = orig.model_dump_json()
        restored = adapter.validate_json(serialized)
        assert restored == orig
        assert restored.event_type == orig.event_type


def test_agent_event_max_length_constraints():
    with pytest.raises(ValidationError):
        AgentMessage(
            run_id="run_1",
            sequence=1,
            timestamp=_ts(1),
            role="assistant",
            content="a" * (AGENT_EVENT_MAX_MESSAGE_CHARS + 1),
        )

    with pytest.raises(ValidationError):
        ToolRequested(
            run_id="run_1",
            sequence=1,
            timestamp=_ts(1),
            tool_name="cmd",
            input_summary="b" * (AGENT_EVENT_MAX_TOOL_SUMMARY_CHARS + 1),
        )

    with pytest.raises(ValidationError):
        AgentCompleted(
            run_id="run_1",
            sequence=1,
            timestamp=_ts(1),
            final_summary="c" * (AGENT_EVENT_MAX_SUMMARY_CHARS + 1),
        )


def test_assert_event_sequence_monotonic():
    events: list[AgentEvent] = [
        AgentStarted(run_id="run_1", sequence=1, timestamp=_ts(1), worker_type="codex"),
        AgentProgress(run_id="run_1", sequence=2, timestamp=_ts(2), phase="running"),
        AgentCompleted(run_id="run_1", sequence=3, timestamp=_ts(3), final_summary="done"),
    ]
    assert_event_sequence_monotonic(events, strictly_consecutive=True)
    assert_event_sequence_monotonic([])
    assert_event_sequence_monotonic([events[0]])

    dup_seq: list[AgentEvent] = [
        AgentStarted(run_id="run_1", sequence=1, timestamp=_ts(1), worker_type="codex"),
        AgentProgress(run_id="run_1", sequence=1, timestamp=_ts(2), phase="running"),
    ]
    with pytest.raises(ValueError, match="Non-monotonic sequence"):
        assert_event_sequence_monotonic(dup_seq)

    dec_seq: list[AgentEvent] = [
        AgentStarted(run_id="run_1", sequence=2, timestamp=_ts(1), worker_type="codex"),
        AgentProgress(run_id="run_1", sequence=1, timestamp=_ts(2), phase="running"),
    ]
    with pytest.raises(ValueError, match="Non-monotonic sequence"):
        assert_event_sequence_monotonic(dec_seq)

    non_consec: list[AgentEvent] = [
        AgentStarted(run_id="run_1", sequence=1, timestamp=_ts(1), worker_type="codex"),
        AgentProgress(run_id="run_1", sequence=3, timestamp=_ts(2), phase="running"),
    ]
    with pytest.raises(ValueError, match="Non-consecutive sequence"):
        assert_event_sequence_monotonic(non_consec, strictly_consecutive=True)

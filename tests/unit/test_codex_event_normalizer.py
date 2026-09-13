from sandbox.redact import SecretRedactor
from workers.agent_event import (
    AgentCompleted,
    AgentFailed,
    AgentMessage,
    AgentProgress,
    AgentStarted,
    BudgetUpdated,
    FileChanged,
    ToolCompleted,
    ToolRequested,
)
from workers.codex_event_normalizer import CodexStreamNormalizer


def test_codex_normalizer_is_known_type():
    norm = CodexStreamNormalizer()
    assert norm.is_known_type({"type": "session_created"}) is True
    assert norm.is_known_type({"event": "task_complete"}) is True
    assert norm.is_known_type({"type": "unknown_future_event"}) is False
    assert norm.is_known_type({}) is False


def test_codex_normalizer_session_created():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "session_created", "command": "codex exec ..."},
        sequence=1,
        run_id="run_1",
    )
    assert isinstance(event, AgentStarted)
    assert event.sequence == 1
    assert event.run_id == "run_1"
    assert event.command == "codex exec ..."


def test_codex_normalizer_reasoning_suppression():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "reasoning", "content": "Secret Chain of Thought thinking..."},
        sequence=2,
        run_id="run_1",
    )
    assert isinstance(event, AgentProgress)
    assert event.phase == "reasoning"
    assert event.message is None  # Critical: reasoning must not leak into message


def test_codex_normalizer_message_delta():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "message_delta", "delta": "Working on file", "phase": "executing"},
        sequence=3,
        run_id="run_1",
    )
    assert isinstance(event, AgentProgress)
    assert event.phase == "executing"
    assert event.message == "Working on file"


def test_codex_normalizer_assistant_message():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "assistant_message", "content": "Fix applied successfully.", "role": "assistant"},
        sequence=4,
        run_id="run_1",
    )
    assert isinstance(event, AgentMessage)
    assert event.role == "assistant"
    assert event.content == "Fix applied successfully."


def test_codex_normalizer_function_call():
    norm = CodexStreamNormalizer()
    # Tool requested
    req = norm.normalize(
        {
            "type": "function_call_begin",
            "name": "edit_file",
            "call_id": "call_abc",
            "arguments": {"path": "main.py"},
        },
        sequence=5,
        run_id="run_1",
    )
    assert isinstance(req, ToolRequested)
    assert req.tool_name == "edit_file"
    assert req.call_id == "call_abc"
    assert "main.py" in (req.input_summary or "")

    # Tool completed
    comp = norm.normalize(
        {
            "type": "function_call_end",
            "name": "edit_file",
            "call_id": "call_abc",
            "exit_code": 0,
            "output": "file updated",
            "duration": 0.45,
        },
        sequence=6,
        run_id="run_1",
    )
    assert isinstance(comp, ToolCompleted)
    assert comp.tool_name == "edit_file"
    assert comp.exit_code == 0
    assert comp.output_summary == "file updated"
    assert comp.duration_seconds == 0.45


def test_codex_normalizer_file_edit():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "file_edit", "path": "src/module.py", "change_kind": "modified"},
        sequence=7,
        run_id="run_1",
    )
    assert isinstance(event, FileChanged)
    assert event.path == "src/module.py"
    assert event.change_kind == "modified"


def test_codex_normalizer_usage():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "usage", "tokens_used": 2048, "cost_usd": 0.05, "context_window_ratio": 0.25},
        sequence=8,
        run_id="run_1",
    )
    assert isinstance(event, BudgetUpdated)
    assert event.tokens_used == 2048
    assert event.cost_usd == 0.05
    assert event.context_window_ratio == 0.25


def test_codex_normalizer_error():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "error", "error": "Disk full", "failure_kind": "system_error", "exit_code": 2},
        sequence=9,
        run_id="run_1",
    )
    assert isinstance(event, AgentFailed)
    assert event.failure_summary == "Disk full"
    assert event.failure_kind == "system_error"
    assert event.exit_code == 2


def test_codex_normalizer_session_complete():
    norm = CodexStreamNormalizer()
    event = norm.normalize(
        {"type": "session_complete", "summary": "All done", "exit_code": 0},
        sequence=10,
        run_id="run_1",
    )
    assert isinstance(event, AgentCompleted)
    assert event.final_summary == "All done"
    assert event.exit_code == 0


def test_codex_normalizer_secret_redaction():
    norm = CodexStreamNormalizer()
    redactor = SecretRedactor({"SUPER_SECRET_TOKEN"})
    event = norm.normalize(
        {"type": "assistant_message", "content": "Leaked token: SUPER_SECRET_TOKEN in code"},
        sequence=11,
        run_id="run_1",
        redactor=redactor,
    )
    assert isinstance(event, AgentMessage)
    assert "SUPER_SECRET_TOKEN" not in event.content
    assert "[REDACTED]" in event.content


def test_codex_normalizer_malformed_and_unknown():
    norm = CodexStreamNormalizer()
    assert norm.normalize({"type": "unknown_type"}, sequence=1, run_id="run_1") is None
    assert norm.normalize({}, sequence=1, run_id="run_1") is None
    # Missing tool name for function_call_begin
    assert norm.normalize({"type": "function_call_begin"}, sequence=1, run_id="run_1") is None
    # Missing path for file_edit
    assert norm.normalize({"type": "file_edit"}, sequence=1, run_id="run_1") is None

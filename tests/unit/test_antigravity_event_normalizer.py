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
from workers.antigravity_event_normalizer import AntigravityStreamNormalizer


def test_antigravity_normalizer_is_known_type():
    norm = AntigravityStreamNormalizer()
    assert norm.is_known_type({"type": "start"}) is True
    assert norm.is_known_type({"event": "complete"}) is True
    assert norm.is_known_type({"type": "unknown_future_event"}) is False
    assert norm.is_known_type({}) is False


def test_antigravity_normalizer_start():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "start", "command": "antigravity --output-format stream-json"},
        sequence=1,
        run_id="run_1",
    )
    assert isinstance(event, AgentStarted)
    assert event.sequence == 1
    assert event.run_id == "run_1"
    assert event.command == "antigravity --output-format stream-json"


def test_antigravity_normalizer_thinking_suppression():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "thinking", "thought": "Internal LLM reasoning process..."},
        sequence=2,
        run_id="run_1",
    )
    assert isinstance(event, AgentProgress)
    assert event.phase == "thinking"
    assert event.message is None  # Critical: thinking must not leak into message


def test_antigravity_normalizer_progress():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "progress", "message": "Analyzing directory", "phase": "investigating"},
        sequence=3,
        run_id="run_1",
    )
    assert isinstance(event, AgentProgress)
    assert event.phase == "investigating"
    assert event.message == "Analyzing directory"


def test_antigravity_normalizer_message():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "message", "content": "Changes are ready.", "role": "assistant"},
        sequence=4,
        run_id="run_1",
    )
    assert isinstance(event, AgentMessage)
    assert event.role == "assistant"
    assert event.content == "Changes are ready."


def test_antigravity_normalizer_tool_call_and_result():
    norm = AntigravityStreamNormalizer()
    # Tool call
    req = norm.normalize(
        {
            "type": "tool_call",
            "name": "run_command",
            "call_id": "call_123",
            "arguments": {"command": "pytest"},
        },
        sequence=5,
        run_id="run_1",
    )
    assert isinstance(req, ToolRequested)
    assert req.tool_name == "run_command"
    assert req.call_id == "call_123"
    assert "pytest" in (req.input_summary or "")

    # Tool result
    comp = norm.normalize(
        {
            "type": "tool_result",
            "name": "run_command",
            "call_id": "call_123",
            "exit_code": 0,
            "output": "15 passed",
            "duration": 1.1,
        },
        sequence=6,
        run_id="run_1",
    )
    assert isinstance(comp, ToolCompleted)
    assert comp.tool_name == "run_command"
    assert comp.exit_code == 0
    assert comp.output_summary == "15 passed"
    assert comp.duration_seconds == 1.1


def test_antigravity_normalizer_file_changed():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "file_changed", "path": "workers/agent_event.py", "change_kind": "added"},
        sequence=7,
        run_id="run_1",
    )
    assert isinstance(event, FileChanged)
    assert event.path == "workers/agent_event.py"
    assert event.change_kind == "added"


def test_antigravity_normalizer_usage():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "usage", "tokens": 4096, "cost": 0.12, "context_window_ratio": 0.50},
        sequence=8,
        run_id="run_1",
    )
    assert isinstance(event, BudgetUpdated)
    assert event.tokens_used == 4096
    assert event.cost_usd == 0.12
    assert event.context_window_ratio == 0.50


def test_antigravity_normalizer_error():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {
            "type": "fatal",
            "message": "Crash in worker",
            "failure_kind": "internal_error",
            "exit_code": 1,
        },
        sequence=9,
        run_id="run_1",
    )
    assert isinstance(event, AgentFailed)
    assert event.failure_summary == "Crash in worker"
    assert event.failure_kind == "internal_error"
    assert event.exit_code == 1


def test_antigravity_normalizer_complete():
    norm = AntigravityStreamNormalizer()
    event = norm.normalize(
        {"type": "complete", "summary": "Finished task", "exit_code": 0},
        sequence=10,
        run_id="run_1",
    )
    assert isinstance(event, AgentCompleted)
    assert event.final_summary == "Finished task"
    assert event.exit_code == 0


def test_antigravity_normalizer_secret_redaction():
    norm = AntigravityStreamNormalizer()
    redactor = SecretRedactor({"API_KEY_GEMINI_TEST"})
    event = norm.normalize(
        {"type": "message", "content": "Key is API_KEY_GEMINI_TEST"},
        sequence=11,
        run_id="run_1",
        redactor=redactor,
    )
    assert isinstance(event, AgentMessage)
    assert "API_KEY_GEMINI_TEST" not in event.content
    assert "[REDACTED]" in event.content


def test_antigravity_normalizer_malformed_and_unknown():
    norm = AntigravityStreamNormalizer()
    assert norm.normalize({"type": "unknown_event"}, sequence=1, run_id="run_1") is None
    assert norm.normalize({}, sequence=1, run_id="run_1") is None
    assert norm.normalize({"type": "tool_call"}, sequence=1, run_id="run_1") is None
    assert norm.normalize({"type": "file_changed"}, sequence=1, run_id="run_1") is None

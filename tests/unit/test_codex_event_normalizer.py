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


def test_codex_normalizer_dotted_lifecycle_and_turns():
    norm = CodexStreamNormalizer()

    # thread.started
    t_start = norm.normalize(
        {"type": "thread.started", "command": "codex exec ..."}, sequence=1, run_id="r1"
    )
    assert isinstance(t_start, AgentStarted)
    assert t_start.command == "codex exec ..."

    # turn.started
    turn_start = norm.normalize({"type": "turn.started"}, sequence=2, run_id="r1")
    assert isinstance(turn_start, AgentProgress)
    assert turn_start.phase == "turn_started"

    # turn.completed with usage
    turn_usage = norm.normalize(
        {"type": "turn.completed", "usage": {"tokens": 120, "cost_usd": 0.02}},
        sequence=3,
        run_id="r1",
    )
    assert isinstance(turn_usage, BudgetUpdated)
    assert turn_usage.tokens_used == 120

    # turn.completed with official Codex breakdown
    codex_usage = norm.normalize(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1500,
                "cached_input_tokens": 1000,
                "output_tokens": 300,
                "reasoning_output_tokens": 150,
            },
        },
        sequence=4,
        run_id="r1",
    )
    assert isinstance(codex_usage, BudgetUpdated)
    assert codex_usage.tokens_used == 1800  # 1500 + 300

    # turn.failed
    turn_fail = norm.normalize(
        {"type": "turn.failed", "failure_summary": "CLI crashed", "exit_code": 1},
        sequence=5,
        run_id="r1",
    )
    assert isinstance(turn_fail, AgentFailed)
    assert turn_fail.exit_code == 1
    assert turn_fail.failure_summary == "CLI crashed"


def test_codex_normalizer_dotted_command_items():
    norm = CodexStreamNormalizer()

    # item.started & completed - command_execution
    cmd_start = norm.normalize(
        {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "git status", "id": "cmd_1"},
        },
        sequence=1,
        run_id="r1",
    )
    assert isinstance(cmd_start, ToolRequested)
    assert cmd_start.tool_name == "execute_bash"
    assert cmd_start.call_id == "cmd_1"
    assert cmd_start.input_summary == "git status"

    cmd_done = norm.normalize(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "output": "clean workspace",
                "exit_code": 0,
                "duration": 0.12,
                "id": "cmd_1",
            },
        },
        sequence=2,
        run_id="r1",
    )
    assert isinstance(cmd_done, ToolCompleted)
    assert cmd_done.exit_code == 0
    assert cmd_done.output_summary == "clean workspace"
    assert cmd_done.duration_seconds == 0.12


def test_codex_normalizer_dotted_other_items():
    norm = CodexStreamNormalizer()

    # item.started & item.completed - reasoning suppression
    reas_start = norm.normalize(
        {"type": "item.started", "item": {"type": "reasoning"}}, sequence=1, run_id="r1"
    )
    assert isinstance(reas_start, AgentProgress)
    assert reas_start.phase == "reasoning"
    assert reas_start.message is None

    reas_done = norm.normalize(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "secret thoughts"}},
        sequence=2,
        run_id="r1",
    )
    assert isinstance(reas_done, AgentProgress)
    assert reas_done.phase == "reasoning"
    assert reas_done.message is None

    # item.started & completed - message
    msg_start = norm.normalize(
        {"type": "item.started", "item": {"type": "message"}}, sequence=3, run_id="r1"
    )
    assert isinstance(msg_start, AgentProgress)
    assert msg_start.phase == "generating"

    msg_done = norm.normalize(
        {
            "type": "item.completed",
            "item": {"type": "message", "content": "Done with task", "role": "assistant"},
        },
        sequence=4,
        run_id="r1",
    )
    assert isinstance(msg_done, AgentMessage)
    assert msg_done.content == "Done with task"

    # item.completed - file_change
    file_done = norm.normalize(
        {
            "type": "item.completed",
            "item": {"type": "file_change", "path": "foo.py", "change_kind": "modified"},
        },
        sequence=5,
        run_id="r1",
    )
    assert isinstance(file_done, FileChanged)
    assert file_done.path == "foo.py"


def test_codex_normalizer_sdk_command_and_files():
    norm = CodexStreamNormalizer()

    # command_execution with item.updated
    cmd_up = norm.normalize(
        {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "command": "npm test",
                "status": "in_progress",
            },
        },
        sequence=1,
        run_id="r1",
    )
    assert isinstance(cmd_up, AgentProgress)
    assert cmd_up.phase == "executing"
    assert cmd_up.message == "npm test"

    # command_execution completed with aggregated_output
    cmd_done = norm.normalize(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "npm test",
                "aggregated_output": "All 42 tests passed",
                "exit_code": 0,
            },
        },
        sequence=2,
        run_id="r1",
    )
    assert isinstance(cmd_done, ToolCompleted)
    assert cmd_done.output_summary == "All 42 tests passed"
    assert cmd_done.exit_code == 0

    # file_change with changes array (add, delete, update)
    files_ev = norm.normalize(
        {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "changes": [
                    {"path": "src/new.py", "kind": "add"},
                    {"path": "src/old.py", "kind": "delete"},
                    {"path": "src/edit.py", "kind": "update"},
                ],
            },
        },
        sequence=3,
        run_id="r1",
    )
    assert isinstance(files_ev, list)
    assert len(files_ev) == 3
    assert [f.path for f in files_ev] == ["src/new.py", "src/old.py", "src/edit.py"]
    assert [f.change_kind for f in files_ev] == ["added", "deleted", "modified"]


def test_codex_normalizer_sdk_auxiliary_items():
    norm = CodexStreamNormalizer()

    # web_search item
    search_ev = norm.normalize(
        {
            "type": "item.started",
            "item": {"type": "web_search", "query": "python typing Protocol"},
        },
        sequence=1,
        run_id="r1",
    )
    assert isinstance(search_ev, AgentProgress)
    assert search_ev.phase == "search"
    assert search_ev.message == "python typing Protocol"

    # todo_list item
    todo_ev = norm.normalize(
        {
            "type": "item.updated",
            "item": {
                "type": "todo_list",
                "items": [
                    {"text": "Task 1", "completed": True},
                    {"text": "Task 2", "completed": False},
                ],
            },
        },
        sequence=2,
        run_id="r1",
    )
    assert isinstance(todo_ev, AgentProgress)
    assert todo_ev.phase == "plan"
    assert todo_ev.message == "1/2 tasks completed"

    # error item
    err_ev = norm.normalize(
        {
            "type": "item.completed",
            "item": {"type": "error", "message": "Failed to resolve dependency"},
        },
        sequence=3,
        run_id="r1",
    )
    assert isinstance(err_ev, AgentProgress)
    assert err_ev.phase == "error"
    assert err_ev.message == "Failed to resolve dependency"

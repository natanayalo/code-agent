# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_run_cli_runtime_loop_executes_git_helper_requests() -> None:
    """Git helper tool calls should be normalized into safe git shell commands."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_git",
                tool_input='{"operation":"status","porcelain":true}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected git status."),
        ]
    )
    session = _FakeSession(
        {
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output=" M tools/registry.py\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "git status --porcelain=v1 --untracked-files=all"
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_git" in execution.messages[1].content
    assert "Tool result: execute_git" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_github_helper_requests() -> None:
    """GitHub helper tool calls should be normalized into safe gh shell commands."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_github",
                tool_input=(
                    '{"operation":"pr_comment","repository_full_name":"openai/code-agent",'
                    '"pr_number":59,"comment_body":"Looks good."}'
                ),
            ),
            CliRuntimeStep(kind="final", final_output="Posted PR comment."),
        ]
    )
    session = _FakeSession(
        {
            "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'": _command_result(
                "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'",
                output="comment-url\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'"
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_github" in execution.messages[1].content
    assert "Tool result: execute_github" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_browser_helper_requests() -> None:
    """Browser helper tool calls should be normalized into safe curl commands."""
    expected_browser_command = (
        "curl --fail --silent --show-error --location "
        f"--max-time={DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS} --globoff --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_browser",
                tool_input='{"operation":"search","query":"langgraph","limit":3}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected search results."),
        ]
    )
    session = _FakeSession(
        {
            expected_browser_command: _command_result(
                expected_browser_command,
                output='["langgraph", ["LangGraph"], ["A framework"], ["https://example.com"]]\n',
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_browser_command
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_browser" in execution.messages[1].content
    assert "Tool result: execute_browser" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_view_file_tool_requests() -> None:
    """View-file tool calls should be normalized into bounded line-number output commands."""
    tool_input = '{"path":"README.md","start_line":2,"end_line":4}'
    expected_command = build_view_file_command_from_input(tool_input)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="view_file", tool_input=tool_input),
            CliRuntimeStep(kind="final", final_output="Viewed README segment."),
        ]
    )
    session = _FakeSession(
        {
            expected_command: _command_result(
                expected_command,
                output="     2  line two\n     3  line three\n     4  line four\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_command
    assert "Tool call: view_file" in execution.messages[1].content
    assert "Tool result: view_file" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_str_replace_editor_tool_requests() -> None:
    """str_replace_editor calls should route through the structured replacement wrapper."""
    tool_input = '{"path":"README.md","old_text":"hello","new_text":"hello world"}'
    expected_command = build_str_replace_editor_command_from_input(tool_input)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="str_replace_editor",
                tool_input=tool_input,
            ),
            CliRuntimeStep(kind="final", final_output="Updated README greeting."),
        ]
    )
    session = _FakeSession(
        {
            expected_command: _command_result(
                expected_command,
                output="",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_command
    assert "Tool call: str_replace_editor" in execution.messages[1].content
    assert "Tool result: str_replace_editor" in execution.messages[2].content


def test_run_cli_runtime_loop_uses_browser_tool_timeout_for_command_rendering() -> None:
    """Browser command generation should use the timeout configured on the tool definition."""
    browser_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_browser").model_copy(
        update={"timeout_seconds": 7}
    )
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(browser_tool,)))
    expected_browser_command = (
        "curl --fail --silent --show-error --location "
        "--max-time=7 --globoff --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_browser",
                tool_input='{"operation":"search","query":"langgraph","limit":3}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected search results."),
        ]
    )
    session = _FakeSession(
        {
            expected_browser_command: _command_result(
                expected_browser_command,
                output='["langgraph", ["LangGraph"], ["A framework"], ["https://example.com"]]\n',
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        tool_client=tool_client,
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert session.calls == [(expected_browser_command, 7)]


def test_run_cli_runtime_loop_rejects_invalid_git_helper_input() -> None:
    """Structured git helper requests should fail clearly on invalid tool input."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_git",
                tool_input='{"operation":"status","message":"bad"}',
            )
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=1, worker_timeout_seconds=30),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "adapter_error"
    assert "invalid input for `execute_git`" in execution.summary
    assert session.calls == []


def test_run_cli_runtime_loop_recovers_from_invalid_str_replace_editor_input() -> None:
    """Invalid str_replace_editor payloads should return feedback and let the adapter recover."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="str_replace_editor",
                tool_input='{"path":"README.md","old_text":"", "new_text":"replacement"}',
            ),
            CliRuntimeStep(kind="final", final_output="Switched to a safer edit approach."),
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.summary == "Switched to a safer edit approach."
    assert session.calls == []
    assert execution.budget_ledger.tool_calls_used == 1
    assert any(
        message.role == "tool"
        and message.tool_name == "str_replace_editor"
        and "input_validation_failed" in message.content
        and "Guidance: for multiline edits, use `execute_bash`" in message.content
        for message in execution.messages
    )

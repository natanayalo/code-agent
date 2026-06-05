"""Additional unit tests to improve coverage for cli_runtime.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workers.cli_runtime import CliRuntimeMessage, CliRuntimeStep


def test_cli_runtime_message_validation() -> None:
    """Verify role-specific validation for CliRuntimeMessage."""
    # Valid messages
    CliRuntimeMessage(role="system", content="sys")
    CliRuntimeMessage(role="assistant", content="ast")
    CliRuntimeMessage(role="tool", content="out", tool_name="ls")

    # Invalid: tool message missing tool_name
    with pytest.raises(ValidationError, match="Tool messages must include tool_name."):
        CliRuntimeMessage(role="tool", content="out")

    # Invalid: non-tool message has tool_name
    with pytest.raises(ValidationError, match="Only tool messages may set tool_name."):
        CliRuntimeMessage(role="system", content="sys", tool_name="ls")


def test_cli_runtime_step_validation() -> None:
    """Verify kind-specific validation for CliRuntimeStep."""
    # Valid steps
    CliRuntimeStep(kind="tool_call", tool_name="ls", tool_input=".")
    CliRuntimeStep(kind="final", final_output="done")

    # Invalid tool_call: missing tool_name
    with pytest.raises(ValidationError, match="Tool calls must target a registered tool name."):
        CliRuntimeStep(kind="tool_call", tool_input=".")

    # Invalid tool_call: empty tool_name
    with pytest.raises(ValidationError, match="Tool calls must target a registered tool name."):
        CliRuntimeStep(kind="tool_call", tool_name=" ", tool_input=".")

    # Invalid tool_call: missing tool_input
    with pytest.raises(ValidationError, match="Tool calls must include a non-empty tool_input."):
        CliRuntimeStep(kind="tool_call", tool_name="ls")

    # Invalid tool_call: has final_output
    with pytest.raises(ValidationError, match="Tool calls cannot include final_output."):
        CliRuntimeStep(kind="tool_call", tool_name="ls", tool_input=".", final_output="fail")

    # Invalid final: missing final_output
    with pytest.raises(ValidationError, match="Final runtime steps must include final_output."):
        CliRuntimeStep(kind="final")

    # Invalid final: has tool fields
    with pytest.raises(
        ValidationError, match="Final runtime steps cannot include tool call fields."
    ):
        CliRuntimeStep(kind="final", final_output="done", tool_name="ls")

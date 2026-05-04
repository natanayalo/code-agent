"""Unit tests for shared runtime-adapter parsing helpers."""

from __future__ import annotations

from workers.adapter_parsing import final_cli_runtime_step, parse_cli_runtime_step_or_final


def test_final_cli_runtime_step_wraps_output() -> None:
    """Arbitrary output should be preserved as a final step payload."""
    step = final_cli_runtime_step("raw output")

    assert step.kind == "final"
    assert step.final_output == "raw output"
    assert step.tool_name is None
    assert step.tool_input is None


def test_parse_cli_runtime_step_or_final_parses_valid_step_json() -> None:
    """Valid CliRuntimeStep payload should be returned unchanged."""
    payload = (
        '{"kind":"tool_call","tool_name":"execute_bash",'
        '"tool_input":"ls -la","final_output":null}'
    )

    step = parse_cli_runtime_step_or_final(payload)

    assert step.kind == "tool_call"
    assert step.tool_name == "execute_bash"
    assert step.tool_input == "ls -la"
    assert step.final_output is None


def test_parse_cli_runtime_step_or_final_falls_back_to_final() -> None:
    """Invalid step payload should become a final output step."""
    payload = '{"reviewer_kind":"worker_self_review","outcome":"no_findings"}'

    step = parse_cli_runtime_step_or_final(payload)

    assert step.kind == "final"
    assert step.final_output == payload
    assert step.tool_name is None
    assert step.tool_input is None

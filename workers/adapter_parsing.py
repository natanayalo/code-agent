"""Shared parsing helpers for runtime adapter outputs."""

from __future__ import annotations

from workers.cli_runtime import CliRuntimeStep


def final_cli_runtime_step(output: str) -> CliRuntimeStep:
    """Wrap arbitrary output as a final runtime step."""
    return CliRuntimeStep(
        kind="final",
        final_output=output,
        tool_name=None,
        tool_input=None,
    )


def parse_cli_runtime_step_or_final(output: str) -> CliRuntimeStep:
    """Parse CliRuntimeStep JSON, falling back to a final-output step."""
    try:
        return CliRuntimeStep.model_validate_json(output)
    except Exception:
        return final_cli_runtime_step(output)

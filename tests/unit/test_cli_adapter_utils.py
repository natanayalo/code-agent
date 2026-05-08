"""Unit tests for shared CLI adapter utility helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from workers.cli_adapter_utils import build_worker_result
from workers.cli_runtime import CliRuntimeExecutionResult, CliRuntimeMessage


def test_build_worker_result_constructs_standardized_output() -> None:
    """The helper should map CLI runtime outputs into a standardized WorkerResult."""
    execution = MagicMock(spec=CliRuntimeExecutionResult)
    execution.status = "success"
    execution.summary = "Execution complete."
    execution.stop_reason = "final_answer"
    execution.commands_run = []
    execution.messages = [CliRuntimeMessage(role="assistant", content="Final answer content.")]
    execution.budget_ledger = MagicMock()
    execution.budget_ledger.model_dump.return_value = {"total_tokens": 100}

    result = build_worker_result(
        execution=execution,
        files_changed=["file1.txt"],
        next_action_hint="inspect_results",
    )

    assert result.status == "success"
    # Deduplication in build_failure_summary should favor the richer message
    # if one contains the other. In this case they are distinct so they concatenate.
    assert result.summary == "Final answer content. Execution complete."
    assert result.files_changed == ["file1.txt"]
    assert result.next_action_hint == "inspect_results"
    assert result.budget_usage == {"total_tokens": 100}


def test_build_worker_result_handles_failure_with_structured_message() -> None:
    """The helper should correctly classify failures using both summary and final message."""
    execution = MagicMock(spec=CliRuntimeExecutionResult)
    execution.status = "failure"
    execution.summary = "Compilation failed."
    execution.stop_reason = "final_answer"
    execution.commands_run = []
    execution.messages = [CliRuntimeMessage(role="assistant", content="Syntax error in main.py")]
    execution.budget_ledger = MagicMock()
    execution.budget_ledger.model_dump.return_value = {"total_tokens": 50}

    result = build_worker_result(
        execution=execution,
        files_changed=[],
    )

    assert result.status == "failure"
    assert result.summary == "Syntax error in main.py Compilation failed."
    # Taxonomy should detect the compile failure from the combined summary
    assert result.failure_kind == "compile"


def test_build_worker_result_deduplicates_messages() -> None:
    """The helper should avoid duplicate content in the final summary."""
    execution = MagicMock(spec=CliRuntimeExecutionResult)
    execution.status = "success"
    execution.summary = "Native run complete."
    execution.stop_reason = "final_answer"
    execution.commands_run = []
    execution.messages = [CliRuntimeMessage(role="assistant", content="Native run complete.")]
    execution.budget_ledger = MagicMock()
    execution.budget_ledger.model_dump.return_value = {}

    result = build_worker_result(
        execution=execution,
        files_changed=[],
    )

    assert result.summary == "Native run complete."

"""Unit tests for native-agent telemetry and durable state sanitization."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from sandbox.redact import SecretRedactor
from workers.base import WorkerResult
from workers.codex_event_normalizer import CodexStreamNormalizer
from workers.native_agent_finalize import _finalize_native_agent_run
from workers.native_agent_runner import NativeAgentRunRequest


def test_finalize_native_agent_run_scrubs_stdout_and_stderr() -> None:
    redactor = SecretRedactor(["sk-super-secret-token-xyz"])
    req = NativeAgentRunRequest(
        command=["codex"],
        prompt="do task",
        repo_path=Path("/tmp/repo"),
        workspace_path=Path("/tmp/ws"),
        normalizer=CodexStreamNormalizer(),
        redactor=redactor,
    )

    stdout_raw = (
        '{"type": "reasoning", "content": "Confidential reasoning steps"}\n'
        '{"type": "thinking", "thought": "Antigravity internal thought"}\n'
        "<thinking>XML chain of thought</thinking>\n"
        "Used auth token: sk-super-secret-token-xyz\n"
    )
    stderr_raw = "Error occurred with token: sk-super-secret-token-xyz and <thought>debug</thought>"

    result = _finalize_native_agent_run(
        request=req,
        status="success",
        summary="Task succeeded",
        command_text="codex",
        started_at=time.perf_counter(),
        timed_out=False,
        stdout=stdout_raw,
        stderr=stderr_raw,
    )

    assert "Confidential reasoning steps" not in result.stdout
    assert "Antigravity internal thought" not in result.stdout
    assert "XML chain of thought" not in result.stdout
    assert "sk-super-secret-token-xyz" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "[REASONING REDACTED]" in result.stdout

    assert "sk-super-secret-token-xyz" not in result.stderr
    assert "debug" not in result.stderr
    assert "[REDACTED]" in result.stderr

    worker_result = WorkerResult(
        status=result.status,
        summary=result.summary,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    serialized = worker_result.model_dump_json()
    assert "Confidential reasoning steps" not in serialized
    assert "Antigravity internal thought" not in serialized
    assert "XML chain of thought" not in serialized
    assert "sk-super-secret-token-xyz" not in serialized
    assert "debug" not in serialized


def test_telemetry_event_capture_enabled_span_attribute() -> None:
    with patch("workers.native_agent_finalize.set_current_span_attribute") as mock_set_attr:
        # Case A: Normalizer set with events_path=None (streaming over stdout)
        req_stdout_streaming = NativeAgentRunRequest(
            command=["codex"],
            prompt="do task",
            repo_path=Path("/tmp/repo"),
            workspace_path=Path("/tmp/ws"),
            normalizer=CodexStreamNormalizer(),
            events_path=None,
        )
        _finalize_native_agent_run(
            request=req_stdout_streaming,
            status="success",
            summary="ok",
            command_text="codex",
            started_at=time.perf_counter(),
            timed_out=False,
        )
        mock_set_attr.assert_any_call("code_agent.native_agent.event_capture_enabled", True)

        # Case B: Normalizer None and events_path None
        mock_set_attr.reset_mock()
        req_disabled = NativeAgentRunRequest(
            command=["codex"],
            prompt="do task",
            repo_path=Path("/tmp/repo"),
            workspace_path=Path("/tmp/ws"),
            normalizer=None,
            events_path=None,
        )
        _finalize_native_agent_run(
            request=req_disabled,
            status="success",
            summary="ok",
            command_text="codex",
            started_at=time.perf_counter(),
            timed_out=False,
        )
        mock_set_attr.assert_any_call("code_agent.native_agent.event_capture_enabled", False)

        # Case C: File events path provided
        mock_set_attr.reset_mock()
        req_file = NativeAgentRunRequest(
            command=["codex"],
            prompt="do task",
            repo_path=Path("/tmp/repo"),
            workspace_path=Path("/tmp/ws"),
            normalizer=None,
            events_path=Path("/tmp/events.jsonl"),
        )
        _finalize_native_agent_run(
            request=req_file,
            status="success",
            summary="ok",
            command_text="codex",
            started_at=time.perf_counter(),
            timed_out=False,
        )
        mock_set_attr.assert_any_call("code_agent.native_agent.event_capture_enabled", True)

"""Unit tests for the native-agent one-shot runner."""

from __future__ import annotations

import json
import stat
import subprocess
import textwrap
from pathlib import Path

import workers.native_agent_artifacts as native_agent_artifacts
import workers.native_agent_finalize as native_finalize
import workers.native_agent_messages as native_agent_messages
import workers.native_agent_runner as native_runner
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent

_UNIFIED_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "assumptions": {"type": "array"},
        "acceptance_criteria": {"type": "array"},
        "non_goals": {"type": "array"},
        "clarification_questions": {"type": "array"},
        "verification_commands": {"type": "array"},
        "suggested_worker": {"type": ["string", "null"]},
        "suggested_profile": {"type": ["string", "null"]},
        "suggested_retry_strategy": {"type": ["object", "null"]},
        "rationale": {"type": ["string", "null"]},
    },
}


def _write_fake_binary(path: Path, body: str) -> Path:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _init_git_repo(repo_path: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    seed_file = repo_path / ".seed"
    if not seed_file.exists():
        seed_file.write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_env_checker_binary(tmp_path: Path) -> Path:
    return _write_fake_binary(
        tmp_path / "fake-env-checker.py",
        """#!/usr/bin/env python3
import os
import json
# Output critical env vars as JSON
print(json.dumps({
    "HOST_VAR": os.environ.get("HOST_VAR"),
    "DATABASE_URL": os.environ.get("DATABASE_URL"),
    "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    "LANG": os.environ.get("LANG"),
    "PATH": os.environ.get("PATH"),
    "HOME": os.environ.get("HOME"),
    "CODE_AGENT_ENABLE_TRACING": os.environ.get("CODE_AGENT_ENABLE_TRACING"),
    "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
    "CODEX_HOME": os.environ.get("CODEX_HOME"),
    "GEMINI_HOME": os.environ.get("GEMINI_HOME"),
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY"),
}))
""",
    )


def test_native_agent_runner_collects_final_message_diff_and_artifacts(tmp_path: Path) -> None:
    """Successful runs should capture final output, git metadata, and artifacts."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    tracked_file = repo_path / "notes.txt"
    tracked_file.write_text("before\n", encoding="utf-8")
    _init_git_repo(repo_path)

    fake_binary = _write_fake_binary(
        tmp_path / "fake-native-agent.py",
        """#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--final-message", required=True)
parser.add_argument("--events", required=True)
parser.add_argument("--touch-file", required=True)
args = parser.parse_args()

prompt = sys.stdin.read()
target = Path(args.touch_file)
target.write_text(target.read_text(encoding="utf-8") + "after\\n", encoding="utf-8")
Path(args.events).write_text('{"event":"turn.completed"}\\n', encoding="utf-8")
Path(args.final_message).write_text(
    json.dumps({"final_output": f"Applied change for: {prompt.strip()}"}),
    encoding="utf-8",
)
print("stdout payload")
print("stderr payload", file=sys.stderr)
""",
    )

    final_message_path = tmp_path / "final-message.json"
    events_path = tmp_path / "events.jsonl"
    result = run_native_agent(
        NativeAgentRunRequest(
            command=[
                str(fake_binary),
                "--final-message",
                str(final_message_path),
                "--events",
                str(events_path),
                "--touch-file",
                str(tracked_file),
            ],
            prompt="implement the task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            final_message_path=final_message_path,
            events_path=events_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.final_message == "Applied change for: implement the task"
    assert result.summary == "Applied change for: implement the task"
    assert result.files_changed == ["notes.txt"]
    assert result.diff_text is not None
    assert "diff --git a/notes.txt b/notes.txt" in result.diff_text
    assert result.stdout.strip() == "stdout payload"
    assert "stderr payload" in result.stderr

    artifact_names = {artifact.name for artifact in result.artifacts}
    assert artifact_names == {
        "native-agent-stdout",
        "native-agent-stderr",
        "native-agent-events",
        "native-agent-final-message",
        "native-agent-diff",
    }
    for artifact in result.artifacts:
        assert Path(artifact.uri.removeprefix("file://")).is_file()


def test_native_agent_runner_handles_non_zero_exit(tmp_path: Path) -> None:
    """Non-zero native command exits should return failure status."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-exit-failure.py",
        """#!/usr/bin/env python3
import sys
print("failure stdout")
print("failure stderr", file=sys.stderr)
raise SystemExit(7)
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "failure"
    assert result.exit_code == 7
    assert result.summary == "Native agent command exited with code 7."
    assert result.final_message == "failure stdout"
    assert len(result.artifacts) == 2
    assert result.artifacts[0].name == "native-agent-stdout"
    assert result.artifacts[1].name == "native-agent-stderr"


def test_native_agent_runner_marks_confirmation_block_as_infra_error(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-confirmation.py",
        """#!/usr/bin/env python3
import sys
print("requires user confirmation", file=sys.stderr)
raise SystemExit(1)
""",
    )
    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )
    assert result.status == "error"
    assert "requires user confirmation" in result.summary


def test_native_agent_runner_marks_tool_not_found_as_infra_error(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-missing-tool.py",
        """#!/usr/bin/env python3
import sys
print("tool foo not found", file=sys.stderr)
raise SystemExit(2)
""",
    )
    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )
    assert result.status == "error"
    assert "tool registry mismatch" in result.summary


def test_native_agent_runner_extracts_json_payload_from_summary_or_stdout(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-json-payload.py",
        """#!/usr/bin/env python3
print('{"final_output":{"status":"passed","summary":"structured"}}')
""",
    )
    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )
    assert result.status == "success"
    assert result.json_payload == {"status": "passed", "summary": "structured"}


def test_json_payload_extraction_prefers_wrapper_response_over_stats() -> None:
    business_payload = {
        "assumptions": [],
        "acceptance_criteria": [],
        "non_goals": [],
        "clarification_questions": [],
        "verification_commands": [],
        "suggested_worker": "antigravity",
        "suggested_profile": "antigravity-native-executor-read-only",
        "suggested_retry_strategy": None,
        "rationale": "read-only task",
    }
    stdout_text = json.dumps(
        {
            "session_id": "session-1",
            "response": json.dumps(business_payload),
            "stats": {
                "models": {"gemini-2.5-pro": {"tokens": {"input": 10, "prompt": 10, "total": 20}}}
            },
        }
    )

    payload, source, rejected_reason = native_runner._extract_business_json_payload(  # noqa: SLF001
        final_message=None,
        stdout_text=stdout_text,
        response_format="json",
        response_schema=_UNIFIED_SUGGESTION_SCHEMA,
    )

    assert payload == business_payload
    assert source == "stdout_wrapper.response"
    assert rejected_reason is None


def test_json_payload_extraction_prefers_fenced_schema_json_over_stats_blob() -> None:
    business_payload = {
        "suggested_worker": "antigravity",
        "suggested_profile": "antigravity-native-executor-read-only",
        "rationale": "use the read-only native profile",
    }
    final_message = "\n".join(
        [
            "```json",
            json.dumps(business_payload),
            "```",
            json.dumps({"input": 10, "prompt": 10, "candidates": 2, "total": 12}),
        ]
    )

    payload, source, rejected_reason = native_runner._extract_business_json_payload(  # noqa: SLF001
        final_message=final_message,
        stdout_text="",
        response_format="json",
        response_schema=_UNIFIED_SUGGESTION_SCHEMA,
    )

    assert payload == business_payload
    assert source == "final_message.fenced_json"
    assert rejected_reason is None


def test_json_payload_extraction_rejects_pure_telemetry_stats() -> None:
    stats_payload = {"input": 10, "prompt": 10, "candidates": 2, "total": 12}

    payload, source, rejected_reason = native_runner._extract_business_json_payload(  # noqa: SLF001
        final_message=json.dumps(stats_payload),
        stdout_text=json.dumps(stats_payload),
        response_format="json",
        response_schema=_UNIFIED_SUGGESTION_SCHEMA,
    )

    assert payload is None
    assert source is None
    assert rejected_reason == "telemetry_only"


def test_native_agent_runner_handles_timeout(tmp_path: Path) -> None:
    """Timeouts should return structured error results."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-timeout.py",
        """#!/usr/bin/env python3
import time
time.sleep(2)
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=1,
        )
    )

    assert result.status == "error"
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.summary == "Native agent command timed out after 1s."
    assert len(result.artifacts) == 2
    assert result.artifacts[0].name == "native-agent-stdout"
    assert result.artifacts[1].name == "native-agent-stderr"


def test_native_agent_runner_truncates_stdout_fallback_summary(tmp_path: Path) -> None:
    """Long stdout fallback summaries should be bounded to prevent payload bloat."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    long_stdout = "x" * (
        native_agent_messages.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS + 250
    )
    fake_binary = _write_fake_binary(
        tmp_path / "fake-long-stdout.py",
        f"""#!/usr/bin/env python3
print("{long_stdout}")
""",
    )

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "success"
    assert result.final_message is not None
    assert result.final_message.startswith("[stdout truncated for summary]")
    assert len(result.final_message) <= (
        len("[stdout truncated for summary]\n")
        + native_agent_messages.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    )
    assert result.final_message.endswith(
        "x" * native_agent_messages.DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    )


def test_read_final_message_is_bounded(tmp_path: Path) -> None:
    """Final message parsing should cap file reads to a fixed safety budget."""
    final_message_path = tmp_path / "final-message.txt"
    oversized = "a" * (native_agent_messages.DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS + 120)
    final_message_path.write_text(oversized, encoding="utf-8")
    parsed = native_agent_messages._read_final_message(final_message_path)

    assert parsed is not None
    assert parsed.endswith("[final message truncated for safety]")
    assert len(parsed) < len(oversized)


def test_extract_final_message_handles_dict_payload_values() -> None:
    payload = '{"final_output":{"status":"passed","summary":"ok"}}'
    assert (
        native_agent_messages._extract_final_message(payload)
        == '{"status": "passed", "summary": "ok"}'
    )  # noqa: SLF001


def test_finalize_native_agent_run_emits_llm_json_output_attribute(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        native_finalize,
        "set_current_span_attribute",
        lambda key, value: captured.append((key, str(value))),
    )
    req = NativeAgentRunRequest(
        command=["echo", "ok"],
        prompt="task",
        repo_path=Path("."),
        workspace_path=Path("."),
    )
    native_runner._finalize_native_agent_run(  # noqa: SLF001
        request=req,
        status="success",
        summary="ok",
        final_message="ok",
        command_text="echo ok",
        exit_code=0,
        started_at=0.0,
        timed_out=False,
        stdout="",
        stderr="",
        json_payload={"status": "passed"},
    )
    assert any(k == "llm.json_output" for k, _ in captured)


def test_finalize_native_agent_run_sets_native_reason_codes(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        native_finalize,
        "set_current_span_attribute",
        lambda key, value: captured.__setitem__(key, value),
    )
    monkeypatch.setattr(native_finalize, "add_current_span_event", lambda name, attributes: None)
    req = NativeAgentRunRequest(
        command=["echo", "ok"],
        prompt="task",
        repo_path=Path("."),
        workspace_path=Path("."),
    )
    native_runner._finalize_native_agent_run(  # noqa: SLF001
        request=req,
        status="failure",
        summary="Native agent command exited with code 41.",
        final_message=None,
        command_text="echo ok",
        exit_code=41,
        started_at=0.0,
        timed_out=False,
        stdout="",
        stderr="",
    )
    assert captured["code_agent.native_agent.outcome_status"] == "failure"
    assert captured["code_agent.native_agent.reason_code"] == "nonzero_exit"
    assert captured["code_agent.native_agent.reason_detail"] == "exit_code_41"
    assert captured["code_agent.native_agent.exit_code"] == 41


def test_finalize_native_agent_run_emits_completed_event(monkeypatch) -> None:
    captured_events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(native_finalize, "set_current_span_attribute", lambda key, value: None)
    monkeypatch.setattr(
        native_finalize,
        "add_current_span_event",
        lambda name, attributes: captured_events.append((name, dict(attributes or {}))),
    )
    req = NativeAgentRunRequest(
        command=["echo", "ok"],
        prompt="task",
        repo_path=Path("."),
        workspace_path=Path("."),
    )
    native_runner._finalize_native_agent_run(  # noqa: SLF001
        request=req,
        status="success",
        summary="ok",
        final_message="ok",
        command_text="echo ok",
        exit_code=0,
        started_at=0.0,
        timed_out=False,
        stdout="",
        stderr="",
        files_changed=["a.py"],
    )
    completed = [
        event for event in captured_events if event[0] == "code_agent.native_agent.run_completed"
    ]
    assert completed
    payload = completed[0][1]
    assert payload["status"] == "success"
    assert payload["reason_code"] == "ok"
    assert payload["has_final_message"] is True
    assert payload["files_changed_count"] == 1


def test_native_agent_runner_returns_structured_error_on_artifact_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Artifact copy/write errors should not crash the caller."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)
    fake_binary = _write_fake_binary(
        tmp_path / "fake-success.py",
        """#!/usr/bin/env python3
print("ok")
""",
    )

    def _raise_artifact_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(native_agent_artifacts, "_write_artifact", _raise_artifact_error)

    result = run_native_agent(
        NativeAgentRunRequest(
            command=[str(fake_binary)],
            prompt="task",
            repo_path=repo_path,
            workspace_path=tmp_path,
            timeout_seconds=10,
        )
    )

    assert result.status == "error"
    assert "failed while collecting artifacts" in result.summary
    assert "disk full" in result.summary


def test_native_agent_runner_git_diff_failure(tmp_path: Path, monkeypatch) -> None:
    """Verify handling of git diff failure."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    class _FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "git error"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeCompleted())

    from workers.native_agent_runner import _collect_diff_text

    assert _collect_diff_text(repo_path=repo_path, timeout_seconds=10) is None


def test_native_agent_runner_git_diff_exception(tmp_path: Path, monkeypatch) -> None:
    """Verify handling of git diff exception."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    def _exploding_run(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", _exploding_run)

    from workers.native_agent_runner import _collect_diff_text

    assert _collect_diff_text(repo_path=repo_path, timeout_seconds=10) is None


def test_native_agent_runner_enforces_strict_isolation(tmp_path: Path, monkeypatch, caplog) -> None:
    """The runner should use a strict allowlist and hide sensitive host variables."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_git_repo(repo_path)

    # Variables that should be hidden
    monkeypatch.setenv("HOST_VAR", "host_value")
    monkeypatch.setenv("DATABASE_URL", "postgresql://real-db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    # Variables that should be allowed
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    fake_binary = _make_env_checker_binary(tmp_path)
    request = NativeAgentRunRequest(
        command=[str(fake_binary)],
        prompt="task",
        repo_path=repo_path,
        workspace_path=tmp_path,
        env={
            "HOME": "/root",  # Should be dropped as protected
            "NEW_SAFE_VAR": "safe_value",
            "CODEX_HOME": "/root/.codex",  # Allowed explicit auth home
            "GEMINI_HOME": "/root/.gemini",  # Allowed explicit auth home
            "GEMINI_API_KEY": "should_be_denied",  # Denied by prefix
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://evil.com",  # Should be denied
            "DATABASE_URL": "mysql://hijacked",  # Should be force-overridden
        },
        timeout_seconds=10,
    )

    result = run_native_agent(request)

    assert result.status == "success"
    payload = result.json_payload
    assert payload is not None

    # Strict isolation checks
    assert payload["HOST_VAR"] is None  # Not in allowlist
    assert payload["AWS_SECRET_ACCESS_KEY"] is None  # Not in allowlist AND prefix denied
    # DATABASE_URL was injected via request.env with "mysql://hijacked" but force-override wins
    assert payload["DATABASE_URL"] is not None
    assert not payload["DATABASE_URL"].startswith("mysql://")  # Hijacked value was suppressed

    # Allowlist checks
    assert payload["LANG"] == "en_US.UTF-8"
    assert payload["PATH"] == "/usr/bin:/bin"

    # HOME redirection check
    assert payload["HOME"].endswith(".agent_home")
    assert (tmp_path / ".agent_home").is_dir()

    # Force-override checks (Must win against request.env and host)
    assert payload["CODE_AGENT_ENABLE_TRACING"] == "0"
    assert payload["TELEGRAM_BOT_TOKEN"] == ""
    assert payload["DATABASE_URL"].startswith("sqlite:///")
    assert ".sandbox.db" in payload["DATABASE_URL"]
    assert payload["HOME"] == str(tmp_path / ".agent_home")
    # Auth home paths are force-set and must not be user-overridable.
    assert payload["CODEX_HOME"] == "/root/.codex"
    assert payload["GEMINI_HOME"] == "/root/.gemini"
    assert payload["GEMINI_API_KEY"] is None

    warning_messages = [
        record.getMessage() for record in caplog.records if record.levelname == "WARNING"
    ]
    assert any(
        "Native agent runner dropped protected environment key: HOME" in message
        for message in warning_messages
    )

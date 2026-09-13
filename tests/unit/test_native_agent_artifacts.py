"""Unit tests for native-agent output artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

from sandbox.redact import SecretRedactor
from workers.native_agent_artifacts import _collect_standard_artifacts
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


def test_collects_redacted_provider_log_artifact(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    provider_log_path = tmp_path / "provider.log"
    provider_log_path.write_text("provider token=super-secret\n", encoding="utf-8")

    artifacts = _collect_standard_artifacts(
        artifact_root=artifact_root,
        stdout_text="stdout\n",
        stderr_text="stderr\n",
        events_path=None,
        provider_log_path=provider_log_path,
        redactor=SecretRedactor(["super-secret"]),
    )

    provider_log = next(
        artifact for artifact in artifacts if artifact.name == "native-agent-provider-log"
    )
    assert provider_log.artifact_type == "log"
    assert Path(provider_log.uri.removeprefix("file://")).read_text(encoding="utf-8") == (
        "provider token=[REDACTED]\n"
    )


def test_provider_log_is_written_directly_to_its_per_run_artifact_path(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    artifact_root = tmp_path / ".code-agent" / "native-agent-runner" / "run-provider-log"
    provider_log_path = artifact_root / "provider.log"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; "
        "Path(sys.argv[sys.argv.index('--log-file') + 1]).write_text('token=secret\\n'); "
        "print('done')",
        "--log-file",
        str(provider_log_path),
    ]

    result = run_native_agent(
        NativeAgentRunRequest(
            command=command,
            prompt="capture provider log",
            repo_path=repo_path,
            workspace_path=tmp_path,
            artifact_root=artifact_root,
            provider_log_path=provider_log_path,
            stdin_prompt=False,
            collect_diff=False,
            collect_changed_files=False,
            redactor=SecretRedactor(["secret"]),
        )
    )

    provider_log = next(
        artifact for artifact in result.artifacts if artifact.name == "native-agent-provider-log"
    )
    assert result.status == "success"
    assert not (tmp_path / ".code-agent" / "antigravity-native.log").exists()
    assert str(provider_log_path) in result.command
    assert Path(provider_log.uri.removeprefix("file://")) == provider_log_path
    assert Path(provider_log.uri.removeprefix("file://")).read_text(encoding="utf-8") == (
        "token=[REDACTED]\n"
    )


def test_stdout_and_stderr_redaction_and_reasoning_scrubbing(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    stdout_input = (
        '{"type": "reasoning", "content": "Private thought"}\n'
        "<thought>Another internal reasoning</thought>\n"
        "Executing with key=super-secret-key\n"
    )
    stderr_input = "Error with key=super-secret-key: <reasoning>debug thought</reasoning>\n"

    artifacts = _collect_standard_artifacts(
        artifact_root=artifact_root,
        stdout_text=stdout_input,
        stderr_text=stderr_input,
        events_path=None,
        provider_log_path=None,
        redactor=SecretRedactor(["super-secret-key"]),
    )

    stdout_art = next(a for a in artifacts if a.name == "native-agent-stdout")
    stderr_art = next(a for a in artifacts if a.name == "native-agent-stderr")

    stdout_text = Path(stdout_art.uri.removeprefix("file://")).read_text(encoding="utf-8")
    stderr_text = Path(stderr_art.uri.removeprefix("file://")).read_text(encoding="utf-8")

    assert "super-secret-key" not in stdout_text
    assert "[REDACTED]" in stdout_text
    assert "Private thought" not in stdout_text
    assert "Another internal reasoning" not in stdout_text
    assert "[REASONING REDACTED]" in stdout_text

    assert "super-secret-key" not in stderr_text
    assert "[REDACTED]" in stderr_text
    assert "debug thought" not in stderr_text
    assert "[REASONING REDACTED]" in stderr_text

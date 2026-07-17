"""Unit tests for native-agent output artifacts."""

from __future__ import annotations

from pathlib import Path

from sandbox.redact import SecretRedactor
from workers.native_agent_artifacts import _collect_standard_artifacts


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

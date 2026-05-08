"""Unit tests for worker system prompt construction."""

from __future__ import annotations

from workers.base import WorkerRequest
from workers.prompt import build_system_prompt


def test_build_system_prompt_does_not_include_native_executor_guidance(
    tmp_path,
) -> None:
    """System prompt should omit redundant native executor anti-tool-selector guidance."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    request = WorkerRequest(
        task_text="Implement feature",
        repo_url="https://example.com/repo.git",
        runtime_mode="native_agent",
        worker_profile="codex-native-executor",
    )

    prompt = build_system_prompt(request, tmp_path)

    assert "## Native Executor Guidance" not in prompt
    assert "tool-call JSON envelopes" not in prompt

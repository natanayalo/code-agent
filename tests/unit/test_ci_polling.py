"""Unit tests for CI polling repair helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.ci_polling import CIPollingScheduler
from apps.api.config import SystemConfig


def _config() -> SystemConfig:
    return SystemConfig(
        default_image="test-image",
        workspace_root="/tmp/code-agent-test",
        ci_polling_enabled=True,
        ci_polling_log_limit_bytes=10,
        ci_polling_llm_profile="none",
    )


def test_fetch_logs_caps_failed_log_output(monkeypatch) -> None:
    """Fetched logs should be capped before repair-task parsing."""
    scheduler = CIPollingScheduler(
        task_service=SimpleNamespace(session_factory=None),
        config=_config(),
    )
    seen_commands: list[list[str]] = []

    def fake_run(command, **_kwargs):  # noqa: ANN001, ANN202
        seen_commands.append(command)
        return SimpleNamespace(returncode=0, stdout="0123456789abcdef")

    monkeypatch.setattr("apps.api.ci_polling.subprocess.run", fake_run)

    logs = scheduler._fetch_logs(  # noqa: SLF001
        "natanayalo/code-agent",
        "abc123",
        {"name": "tests", "link": "https://github.com/r/actions/runs/123"},
        {},
    )

    assert logs == "6789abcdef"
    assert seen_commands == [
        ["gh", "run", "view", "123", "--log-failed", "-R", "natanayalo/code-agent"]
    ]


def test_submit_repair_task_uses_idempotency_key_and_repair_link(monkeypatch) -> None:
    """Repair submissions should carry both dedupe and source-task links."""
    recorded: dict[str, object] = {}

    def create_task_outcome(submission, *, delivery_key):  # noqa: ANN001, ANN202
        recorded["submission"] = submission
        recorded["delivery_key"] = delivery_key
        return SimpleNamespace(
            duplicate=False,
            task_snapshot=SimpleNamespace(task_id="repair-task-1"),
        )

    scheduler = CIPollingScheduler(
        task_service=SimpleNamespace(
            session_factory=None,
            create_task_outcome=create_task_outcome,
            worker=None,
        ),
        config=_config(),
    )
    monkeypatch.setattr(scheduler, "_fetch_logs", lambda *_args, **_kwargs: None)

    scheduler._submit_repair_task(  # noqa: SLF001
        "source-task",
        "https://github.com/natanayalo/code-agent",
        "natanayalo/code-agent",
        "task/source-task",
        "abc123",
        {"name": "tests", "link": "https://github.com/r/actions/runs/123"},
        {},
    )

    submission = recorded["submission"]
    delivery_key = recorded["delivery_key"]
    assert submission.repair_for_task_id == "source-task"
    assert submission.repo_url == "https://github.com/natanayalo/code-agent"
    assert delivery_key.delivery_id == "ci_repair:source-task:abc123:tests"


@pytest.mark.anyio
async def test_parse_logs_with_llm_async_uses_facade() -> None:
    """LLM parsing should resolve worker from facade when profile is enabled."""
    worker_mock = AsyncMock()
    worker_mock.run.return_value = SimpleNamespace(status="success", summary="parsed fail")

    facade_mock = SimpleNamespace(
        get_worker=lambda worker_type: worker_mock if worker_type == "antigravity" else None
    )

    scheduler = CIPollingScheduler(
        task_service=SimpleNamespace(worker=facade_mock),
        config=SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            ci_polling_enabled=True,
            ci_polling_llm_profile="default",
        ),
    )

    result = await scheduler._parse_logs_with_llm_async("raw logs", "test_job")
    assert result == "parsed fail"

    worker_mock.run.assert_called_once()
    req = worker_mock.run.call_args[0][0]
    assert req.worker_profile == "default"
    assert " raw logs" in req.task_text or "raw logs" in req.task_text

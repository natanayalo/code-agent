from __future__ import annotations

import pytest

from orchestrator.nodes.utils import _available_workers
from workers import WorkerRequest


@pytest.mark.anyio
async def test_available_workers_includes_shell_and_default_worker() -> None:
    workers = _available_workers(shell_worker=object())  # type: ignore[arg-type]
    assert "codex" in workers
    assert "shell" in workers

    result = await workers["codex"].run(WorkerRequest(task_text="demo"))
    assert result.status == "success"
    assert "Fake worker completed" in (result.summary or "")

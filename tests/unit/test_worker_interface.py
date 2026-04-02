"""Unit tests for the shared worker contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workers import WorkerRequest, WorkerResult


def test_worker_request_supports_contract_fields() -> None:
    """Worker request models accept the documented contract fields."""
    request = WorkerRequest(
        session_id="session-1",
        repo_url="https://github.com/natanayalo/code-agent",
        branch="task/t-040-worker-interface",
        task_text="Define worker interface",
        memory_context={"project": [{"memory_key": "pitfall"}]},
        constraints={"requires_approval": False},
        budget={"max_minutes": 15},
    )

    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.task_text == "Define worker interface"


def test_worker_request_rejects_unknown_fields() -> None:
    """Worker request models stay inspectable by rejecting extra data."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorkerRequest(task_text="Define worker interface", unexpected="value")


def test_worker_result_requires_known_status_values() -> None:
    """Worker result models reject unsupported status strings."""
    with pytest.raises(ValidationError, match="Input should be"):
        WorkerResult(status="unknown")

"""Unit tests for worker-produced memory contract fields."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from workers import WorkerMemoryEntry, WorkerResult


def test_worker_result_accepts_memory_to_persist() -> None:
    """WorkerResult should validate typed memory entries for orchestrator persistence."""
    verified_at = datetime(2026, 7, 2, tzinfo=UTC)

    result = WorkerResult(
        status="success",
        summary="learned something useful",
        memory_to_persist=[
            WorkerMemoryEntry(
                category="project",
                memory_key="test_command",
                value={"command": ".venv/bin/pytest tests/unit"},
                repo_url="https://github.com/natanayalo/code-agent",
                source="worker_result",
                confidence=0.8,
                scope="repo",
                last_verified_at=verified_at,
                requires_verification=False,
            )
        ],
    )

    assert result.memory_to_persist[0].memory_key == "test_command"
    assert result.memory_to_persist[0].last_verified_at == verified_at


def test_worker_memory_rejects_invalid_category() -> None:
    """Worker memory entries should stay within the v1 personal/project vocabulary."""
    with pytest.raises(ValidationError):
        WorkerMemoryEntry(
            category="session",
            memory_key="unsupported",
            value={},
        )


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_worker_memory_confidence_bounds(confidence: float) -> None:
    """Confidence must remain normalized between 0 and 1."""
    with pytest.raises(ValidationError):
        WorkerMemoryEntry(
            category="personal",
            memory_key="communication",
            value={"style": "concise"},
            confidence=confidence,
        )

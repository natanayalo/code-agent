"""Unit tests for ScoutScheduler in apps/api/scheduler.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from apps.api.config import SystemConfig
from apps.api.scheduler import ScoutScheduler


@pytest.mark.asyncio
async def test_scout_scheduler_start_stop():
    task_service = MagicMock()
    config_disabled = SystemConfig(
        default_image="img", workspace_root="/tmp", scout_scheduler_enabled=False
    )
    scheduler = ScoutScheduler(task_service, config_disabled)

    # Start when disabled
    scheduler.start()
    assert scheduler._running is False

    # Start when enabled
    config_enabled = SystemConfig(
        default_image="img", workspace_root="/tmp", scout_scheduler_enabled=True
    )
    scheduler_enabled = ScoutScheduler(task_service, config_enabled)
    scheduler_enabled.start()
    assert scheduler_enabled._running is True

    # Double start is no-op
    scheduler_enabled.start()

    # Stop
    await scheduler_enabled.stop()
    assert scheduler_enabled._running is False
    assert scheduler_enabled._task is None


@pytest.mark.asyncio
async def test_scout_scheduler_loop():
    task_service = MagicMock()
    config = SystemConfig(default_image="img", workspace_root="/tmp", scout_scheduler_enabled=True)
    scheduler = ScoutScheduler(task_service, config)

    scheduler._running = True
    with (
        patch.object(scheduler, "tick") as mock_tick,
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
    ):
        mock_tick.side_effect = Exception("Tick error")
        await scheduler._loop()
        mock_tick.assert_called_once()


def test_scout_scheduler_tick_disabled_or_no_key():
    task_service = MagicMock()
    config1 = SystemConfig(
        default_image="img", workspace_root="/tmp", scout_scheduler_enabled=False, scout_repo_key=""
    )
    scheduler1 = ScoutScheduler(task_service, config1)
    now = datetime.now(UTC)

    scheduler1.tick(now)
    task_service.is_execution_busy.assert_not_called()

    config2 = SystemConfig(
        default_image="img", workspace_root="/tmp", scout_scheduler_enabled=True, scout_repo_key=""
    )
    scheduler2 = ScoutScheduler(task_service, config2)
    scheduler2.tick(now)
    task_service.is_execution_busy.assert_not_called()


def test_scout_scheduler_tick_busy():
    task_service = MagicMock()
    task_service.is_execution_busy.return_value = True
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_key="repo-key",
    )
    scheduler = ScoutScheduler(task_service, config)
    now = datetime.now(UTC)

    scheduler.tick(now)
    assert scheduler._last_busy_time == now


def test_scout_scheduler_tick_triggers():
    task_service = MagicMock()
    task_service.is_execution_busy.return_value = False

    outcome = MagicMock()
    outcome.duplicate = False
    task_service.create_task_outcome.return_value = outcome

    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_key="my-repo",
        scout_schedule_interval_minutes=60,
        scout_idle_trigger_minutes=15,
        scout_task_text="Perform scout audit",
        scout_branch="main",
    )
    with patch(
        "apps.api.config.SystemConfig.resolve_repo_key",
        return_value="https://github.com/org/my-repo",
    ):
        scheduler = ScoutScheduler(task_service, config)
        now = datetime.now(UTC)
        scheduler._last_busy_time = now - timedelta(minutes=30)

        # 1. Schedule trigger
        scheduler.tick(now)
        assert task_service.create_task_outcome.called

        # 2. Idle trigger when schedule period unchanged
        task_service.create_task_outcome.reset_mock()
        scheduler.tick(now + timedelta(seconds=1))
        assert task_service.create_task_outcome.called


def test_scout_scheduler_submit_scout_failures():
    task_service = MagicMock()
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_key="invalid-key",
    )
    scheduler = ScoutScheduler(task_service, config)

    with patch("apps.api.config.SystemConfig.resolve_repo_key", return_value=None):
        deliv = MagicMock()
        assert scheduler._submit_scout(deliv, trigger_source="schedule") is False

    with patch(
        "apps.api.config.SystemConfig.resolve_repo_key", return_value="https://github.com/org/repo"
    ):
        task_service.create_task_outcome.side_effect = RuntimeError("DB error")
        assert scheduler._submit_scout(deliv, trigger_source="schedule") is False

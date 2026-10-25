"""Unit tests for GitHub issue task replay (/code-agent replay) (M30)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from apps.api.config import SystemConfig
from orchestrator.execution_types import TaskReplayResult, TaskSnapshot
from orchestrator.github_issue_intake import (
    handle_issue_intake,
    parse_issue_command_intent,
)


def test_parse_issue_command_intent():
    assert parse_issue_command_intent(None) == ("create", None)
    assert parse_issue_command_intent("") == ("create", None)
    assert parse_issue_command_intent("   ") == ("create", None)
    assert parse_issue_command_intent("replay") == ("replay", None)
    assert parse_issue_command_intent("REPLAY") == ("replay", None)
    assert parse_issue_command_intent("  replay  ") == ("replay", None)
    assert parse_issue_command_intent("replay with debug") == ("replay", "with debug")
    assert parse_issue_command_intent("replay-task") == ("create", "replay-task")
    assert parse_issue_command_intent("fix the login bug") == ("create", "fix the login bug")


def test_handle_issue_replay_unit_duplicate_delivery():
    mock_service = MagicMock()
    mock_session = MagicMock()
    mock_service.session_factory.return_value.__enter__.return_value = mock_session

    existing = MagicMock()
    existing.task_id = "task-existing"
    task_mock = MagicMock()
    task_mock.session_id = "session-1"

    with (
        patch("orchestrator.github_issue_intake.InboundDeliveryRepository") as mock_del_cls,
        patch("orchestrator.github_issue_intake.TaskRepository") as mock_task_cls,
    ):
        mock_del_cls.return_value.get_by_channel_delivery.return_value = existing
        mock_task_cls.return_value.get.return_value = task_mock

        config = SystemConfig(
            default_image="img",
            workspace_root="/tmp",
            github_owner_login="octocat",
            github_issue_label="code-agent",
        )
        payload = {
            "issue": {"number": 42},
            "comment": {"body": "/code-agent replay"},
            "sender": {"login": "octocat"},
        }
        ok, status, dupe, task_id, sess_id, detail = handle_issue_intake(
            payload=payload,
            event="issue_comment",
            action="created",
            delivery_id="del-dupe",
            repo_url="https://github.com/octocat/code-agent",
            config=config,
            task_service=mock_service,
        )
        assert ok is True
        assert status == "duplicate"
        assert dupe is True
        assert task_id == "task-existing"
        assert sess_id == "session-1"


def test_handle_issue_replay_unit_not_found():
    mock_service = MagicMock()
    mock_session = MagicMock()
    mock_service.session_factory.return_value.__enter__.return_value = mock_session

    with (
        patch("orchestrator.github_issue_intake.InboundDeliveryRepository") as mock_del_cls,
        patch("orchestrator.github_issue_intake.SessionRepository") as mock_sess_cls,
    ):
        mock_del_cls.return_value.get_by_channel_delivery.return_value = None
        mock_sess_cls.return_value.get_by_channel_thread.return_value = None

        config = SystemConfig(
            default_image="img",
            workspace_root="/tmp",
            github_owner_login="octocat",
            github_issue_label="code-agent",
        )
        payload = {
            "issue": {"number": 42},
            "comment": {"body": "/code-agent replay"},
            "sender": {"login": "octocat"},
        }
        ok, status, dupe, task_id, sess_id, detail = handle_issue_intake(
            payload=payload,
            event="issue_comment",
            action="created",
            delivery_id="del-nf",
            repo_url="https://github.com/octocat/code-agent",
            config=config,
            task_service=mock_service,
        )
        assert ok is False
        assert status == "not_found"
        assert dupe is False
        assert task_id is None
        assert "No prior task found" in (detail or "")


def test_handle_issue_replay_unit_not_replayable():
    mock_service = MagicMock()
    mock_session = MagicMock()
    mock_service.session_factory.return_value.__enter__.return_value = mock_session

    conv_mock = MagicMock()
    conv_mock.id = "session-1"
    task_mock = MagicMock()
    task_mock.id = "task-prior"

    with (
        patch("orchestrator.github_issue_intake.InboundDeliveryRepository") as mock_del_cls,
        patch("orchestrator.github_issue_intake.SessionRepository") as mock_sess_cls,
        patch("orchestrator.github_issue_intake.TaskRepository") as mock_task_cls,
    ):
        mock_del_cls.return_value.get_by_channel_delivery.return_value = None
        mock_sess_cls.return_value.get_by_channel_thread.return_value = conv_mock
        mock_task_cls.return_value.list_by_session.return_value = [task_mock]

        mock_service.replay_task.return_value = TaskReplayResult(
            status="not_replayable",
            source_task_id="task-prior",
            detail="Task is currently running and cannot be replayed.",
        )

        config = SystemConfig(
            default_image="img",
            workspace_root="/tmp",
            github_owner_login="octocat",
            github_issue_label="code-agent",
        )
        payload = {
            "issue": {"number": 42},
            "comment": {"body": "/code-agent replay"},
            "sender": {"login": "octocat"},
        }
        ok, status, dupe, task_id, sess_id, detail = handle_issue_intake(
            payload=payload,
            event="issue_comment",
            action="created",
            delivery_id="del-nr",
            repo_url="https://github.com/octocat/code-agent",
            config=config,
            task_service=mock_service,
        )
        assert ok is False
        assert status == "not_replayable"
        assert dupe is False
        assert sess_id == "session-1"
        assert "cannot be replayed" in (detail or "")


def test_handle_issue_replay_unit_success():
    mock_service = MagicMock()
    mock_session = MagicMock()
    mock_service.session_factory.return_value.__enter__.return_value = mock_session

    conv_mock = MagicMock()
    conv_mock.id = "session-1"
    task_mock = MagicMock()
    task_mock.id = "task-source-100"

    with (
        patch("orchestrator.github_issue_intake.InboundDeliveryRepository") as mock_del_cls,
        patch("orchestrator.github_issue_intake.SessionRepository") as mock_sess_cls,
        patch("orchestrator.github_issue_intake.TaskRepository") as mock_task_cls,
        patch("orchestrator.github_issue_intake._record_ingested_timeline_event") as mock_timeline,
    ):
        delivery_repo_inst = MagicMock()
        delivery_repo_inst.get_by_channel_delivery.return_value = None
        mock_del_cls.return_value = delivery_repo_inst
        mock_sess_cls.return_value.get_by_channel_thread.return_value = conv_mock
        mock_task_cls.return_value.list_by_session.return_value = [task_mock]

        now = datetime.now(UTC)
        new_snapshot = TaskSnapshot(
            task_id="task-replayed-200",
            session_id="session-1",
            status="pending",
            task_text="Issue #42",
            created_at=now,
            updated_at=now,
        )
        mock_service.replay_task.return_value = TaskReplayResult(
            status="created",
            source_task_id="task-source-100",
            task_snapshot=new_snapshot,
        )

        config = SystemConfig(
            default_image="img",
            workspace_root="/tmp",
            github_owner_login="octocat",
            github_issue_label="code-agent",
        )
        payload = {
            "issue": {"number": 42, "title": "Crash fix", "id": 1234},
            "comment": {"id": 9999, "body": "/code-agent replay"},
            "sender": {"login": "octocat"},
        }
        ok, status, dupe, task_id, sess_id, detail = handle_issue_intake(
            payload=payload,
            event="issue_comment",
            action="created",
            delivery_id="del-ok-replay",
            repo_url="https://github.com/octocat/code-agent",
            config=config,
            task_service=mock_service,
        )
        assert ok is True
        assert status == "accepted"
        assert dupe is False
        assert task_id == "task-replayed-200"
        assert sess_id == "session-1"

        delivery_repo_inst.create.assert_called_once_with(
            channel="github",
            delivery_id="del-ok-replay",
            task_id="task-replayed-200",
        )
        mock_timeline.assert_called_once()
        _, kwargs = mock_timeline.call_args
        assert kwargs["task_id"] == "task-replayed-200"
        assert kwargs["github_constraints"]["replayed_from"] == "task-source-100"
        assert kwargs["github_constraints"]["issue_number"] == 42

"""Pending claim reconciliation uses the existing atomic attach operation."""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from orchestrator.github_delivery_sync import _record_delivery_claim, handle_delivery_sync


@pytest.mark.parametrize("task_id", [None, "task-1"])
def test_existing_claim_attaches_only_after_metadata_is_available(task_id):
    session, repository = MagicMock(), MagicMock()
    repository.create.side_effect = IntegrityError("synthetic duplicate", {}, Exception())
    _record_delivery_claim(session, repository, "delivery-1", task_id=task_id)
    if task_id is None:
        repository.attach_task_if_unassigned.assert_not_called()
    else:
        repository.attach_task_if_unassigned.assert_called_once_with(
            channel="github", delivery_id="delivery-1", task_id=task_id
        )


def test_handle_delivery_sync_pending_when_worker_run_lacks_delivery_metadata():
    task_service = MagicMock()
    mock_session = MagicMock()
    task_service.session_factory.return_value = mock_session

    with (
        patch("orchestrator.github_delivery_sync.session_scope") as mock_scope,
        patch("orchestrator.github_delivery_sync.InboundDeliveryRepository") as mock_repo_cls,
        patch("orchestrator.github_delivery_sync.find_matching_delivery_run") as mock_find,
        patch("orchestrator.github_delivery_sync._record_delivery_claim") as mock_record,
    ):
        mock_scope.return_value.__enter__.return_value = mock_session
        delivery_repo = mock_repo_cls.return_value
        delivery_repo.get_by_channel_delivery.return_value = None

        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_run = MagicMock()
        mock_run.delivery_metadata = None
        mock_find.return_value = (mock_run, mock_task)

        ok, status, dup, task_id, run_id, detail = handle_delivery_sync(
            payload={"action": "opened", "pull_request": {"number": 1}},
            event="pull_request",
            action="opened",
            delivery_id="del-123",
            repo_url="https://github.com/octocat/code-agent",
            task_service=task_service,
        )

        assert ok is True
        assert status == "pending"
        assert dup is False
        assert task_id is None
        mock_record.assert_called_once_with(mock_session, delivery_repo, "del-123", task_id=None)


def test_handle_delivery_sync_ignored_does_not_attach_task_id_to_claim():
    task_service = MagicMock()
    mock_session = MagicMock()
    task_service.session_factory.return_value = mock_session

    with (
        patch("orchestrator.github_delivery_sync.session_scope") as mock_scope,
        patch("orchestrator.github_delivery_sync.InboundDeliveryRepository") as mock_repo_cls,
        patch("orchestrator.github_delivery_sync.find_matching_delivery_run") as mock_find,
        patch("orchestrator.github_delivery_sync._apply_worker_run_delivery_update") as mock_update,
        patch(
            "orchestrator.github_delivery_sync.record_delivery_timeline_event"
        ) as mock_record_event,
        patch("orchestrator.github_delivery_sync._record_delivery_claim") as mock_record_claim,
    ):
        mock_scope.return_value.__enter__.return_value = mock_session
        delivery_repo = mock_repo_cls.return_value
        delivery_repo.get_by_channel_delivery.return_value = None

        mock_task = MagicMock()
        mock_task.id = "task-1"
        mock_run = MagicMock()
        mock_run.delivery_metadata = {"head_sha": "B"}
        mock_find.return_value = (mock_run, mock_task)
        mock_update.return_value = ({"head_sha": "A"}, False)

        ok, status, dup, task_id, run_id, detail = handle_delivery_sync(
            payload={"action": "completed", "check_run": {"head_sha": "A"}},
            event="check_run",
            action="completed",
            delivery_id="del-ignored",
            repo_url="https://github.com/octocat/code-agent",
            task_service=task_service,
        )

        assert ok is True
        assert status == "ignored"
        assert dup is False
        assert task_id == "task-1"
        mock_record_event.assert_not_called()
        mock_record_claim.assert_not_called()

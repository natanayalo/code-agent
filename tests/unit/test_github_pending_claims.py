"""Pending claim reconciliation uses the existing atomic attach operation."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from orchestrator.github_delivery_sync import _record_delivery_claim


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

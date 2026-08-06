"""Unit tests for proposal management API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from apps.api.routes.proposals import accept_proposal, list_proposals, reject_proposal
from db.enums import ProposalStatus, ProposalType


def test_list_proposals():
    task_service = MagicMock()
    task_service.list_proposals.return_value = []
    res = list_proposals(
        status_filter=ProposalStatus.PENDING_REVIEW,
        proposal_type=ProposalType.SCOUT,
        session_id="s1",
        task_id="t1",
        limit=10,
        offset=0,
        task_service=task_service,
    )
    assert res == []
    task_service.list_proposals.assert_called_once_with(
        status=ProposalStatus.PENDING_REVIEW,
        proposal_type=ProposalType.SCOUT,
        session_id="s1",
        task_id="t1",
        limit=10,
        offset=0,
    )


def test_accept_proposal_success():
    task_service = MagicMock()
    mock_snapshot = MagicMock()
    task_service.accept_proposal.return_value = ("ok", mock_snapshot, None)
    pid = uuid4()
    assert accept_proposal(pid, task_service=task_service) == mock_snapshot


def test_accept_proposal_not_found():
    task_service = MagicMock()
    task_service.accept_proposal.return_value = ("not_found", None, "Missing")
    with pytest.raises(HTTPException) as exc_info:
        accept_proposal(uuid4(), task_service=task_service)
    assert exc_info.value.status_code == 404


def test_accept_proposal_conflict():
    task_service = MagicMock()
    mock_snapshot = MagicMock()
    # Conflict with snapshot returns snapshot
    task_service.accept_proposal.return_value = ("conflict", mock_snapshot, None)
    pid = uuid4()
    assert accept_proposal(pid, task_service=task_service) == mock_snapshot

    # Conflict without snapshot raises 409
    task_service.accept_proposal.return_value = ("conflict", None, "Cannot accept")
    with pytest.raises(HTTPException) as exc_info:
        accept_proposal(pid, task_service=task_service)
    assert exc_info.value.status_code == 409


def test_accept_proposal_internal_error():
    task_service = MagicMock()
    task_service.accept_proposal.return_value = ("ok", None, None)
    with pytest.raises(HTTPException) as exc_info:
        accept_proposal(uuid4(), task_service=task_service)
    assert exc_info.value.status_code == 500


def test_reject_proposal_success():
    task_service = MagicMock()
    mock_snapshot = MagicMock()
    task_service.reject_proposal.return_value = ("ok", mock_snapshot, None)
    pid = uuid4()
    assert reject_proposal(pid, task_service=task_service) == mock_snapshot


def test_reject_proposal_not_found():
    task_service = MagicMock()
    task_service.reject_proposal.return_value = ("not_found", None, "Missing")
    with pytest.raises(HTTPException) as exc_info:
        reject_proposal(uuid4(), task_service=task_service)
    assert exc_info.value.status_code == 404


def test_reject_proposal_conflict():
    task_service = MagicMock()
    task_service.reject_proposal.return_value = ("conflict", None, "Already rejected")
    with pytest.raises(HTTPException) as exc_info:
        reject_proposal(uuid4(), task_service=task_service)
    assert exc_info.value.status_code == 409


def test_reject_proposal_internal_error():
    task_service = MagicMock()
    task_service.reject_proposal.return_value = ("ok", None, None)
    with pytest.raises(HTTPException) as exc_info:
        reject_proposal(uuid4(), task_service=task_service)
    assert exc_info.value.status_code == 500

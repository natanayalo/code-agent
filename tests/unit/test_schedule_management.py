"""Failure and recovery paths for Temporal schedule management."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from apps.api.config import SystemConfig
from apps.api.routes.schedules import (
    _identity,
    _recent_action,
    create_schedule,
    get_schedule_client,
    list_schedules,
    manage_schedule,
)
from orchestrator.scheduled_submission import lock_and_check_occurrence
from tests.unit.test_schedule_spec import valid_spec


def _rpc(status):
    return RPCError("test failure", status, b"")


@pytest.mark.anyio
async def test_connection_failure_returns_503(monkeypatch):
    monkeypatch.setattr(
        "apps.api.routes.schedules.Client.connect", AsyncMock(side_effect=OSError())
    )
    with pytest.raises(HTTPException) as error:
        await get_schedule_client()
    assert error.value.status_code == 503


@pytest.mark.parametrize("value", ["bad space", "x" * 201, "nonascii-é", ""])
def test_schedule_identity_rejects_invalid_ids(value):
    with pytest.raises(HTTPException) as error:
        _identity(value)
    assert error.value.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(WorkflowExecutionStatus.RUNNING, "running"), (WorkflowExecutionStatus.FAILED, "failed")],
)
async def test_recent_action_reports_status(status, expected):
    handle = SimpleNamespace(describe=AsyncMock(return_value=SimpleNamespace(status=status)))
    client = SimpleNamespace(get_workflow_handle=lambda *args, **kwargs: handle)
    action = SimpleNamespace(
        action=SimpleNamespace(workflow_id="occurrence", first_execution_run_id="run"),
        scheduled_at=None,
        started_at=None,
    )
    assert (await _recent_action(client, action))["outcome"] == expected


@pytest.mark.anyio
@pytest.mark.parametrize("status", [RPCStatusCode.NOT_FOUND, RPCStatusCode.UNAVAILABLE])
async def test_recent_action_handles_expired_history_and_unavailability(status):
    handle = SimpleNamespace(describe=AsyncMock(side_effect=_rpc(status)))
    client = SimpleNamespace(get_workflow_handle=lambda *args, **kwargs: handle)
    action = SimpleNamespace(
        action=SimpleNamespace(workflow_id="occurrence", first_execution_run_id="run"),
        scheduled_at=None,
        started_at=None,
    )
    if status == RPCStatusCode.NOT_FOUND:
        assert (await _recent_action(client, action))["outcome"] == "history_expired"
    else:
        with pytest.raises(HTTPException) as error:
            await _recent_action(client, action)
        assert error.value.status_code == 503


@pytest.mark.anyio
async def test_schedule_operations_fail_closed():
    client = SimpleNamespace(list_schedules=AsyncMock(side_effect=_rpc(RPCStatusCode.UNAVAILABLE)))
    with pytest.raises(HTTPException) as error:
        await list_schedules(client)
    assert error.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        await manage_schedule("id", "unknown", client)
    assert error.value.status_code == 404
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=object()),
        pause=AsyncMock(side_effect=_rpc(RPCStatusCode.UNAVAILABLE)),
    )
    client.get_schedule_handle = lambda _: handle
    with pytest.raises(HTTPException) as error:
        await manage_schedule("id", "pause", client)
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_create_reports_temporal_failure():
    client = SimpleNamespace(create_schedule=AsyncMock(side_effect=_rpc(RPCStatusCode.UNAVAILABLE)))
    config = SystemConfig(
        default_image="test", workspace_root="/tmp", allowed_repos={"project": "repo"}
    )
    with pytest.raises(HTTPException) as error:
        await create_schedule("id", valid_spec(), config, client)
    assert error.value.status_code == 503


def test_scheduled_lock_requires_delivery_key():
    session = SimpleNamespace(execute=lambda _: None)
    with pytest.raises(ValueError, match="delivery key"):
        lock_and_check_occurrence(session, "id", None, None)

"""Unit tests for orchestrator/temporal/command_dispatcher.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import temporalio.exceptions

from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher


@pytest.mark.asyncio
async def test_dispatcher_deliver():
    client = MagicMock()
    client.start_workflow = AsyncMock()
    handle = MagicMock()
    handle.signal = AsyncMock()
    handle.cancel = AsyncMock()
    client.get_workflow_handle.return_value = handle

    dispatcher = TemporalCommandDispatcher(client=client, session_factory=MagicMock())

    # start
    await dispatcher._deliver(task_id="t1", command_type="start", command_key="k1", payload={})
    client.start_workflow.assert_called_once()

    # start already started
    client.start_workflow.side_effect = temporalio.exceptions.WorkflowAlreadyStartedError(
        workflow_id="task-t1",
        workflow_type="TaskExecutionWorkflow",
        run_id="r1",
    )
    await dispatcher._deliver(task_id="t1", command_type="start", command_key="k1", payload={})

    # signal
    await dispatcher._deliver(
        task_id="t1",
        command_type="signal",
        command_key="k2",
        payload={"signal_name": "sig1", "signal_arg": True},
    )
    handle.signal.assert_called_once()

    # cancel
    await dispatcher._deliver(task_id="t1", command_type="cancel", command_key="k3", payload={})
    handle.cancel.assert_called_once()

    # unknown
    with pytest.raises(ValueError, match="Unknown Temporal command type"):
        await dispatcher._deliver(
            task_id="t1", command_type="unknown", command_key="k4", payload={}
        )


@pytest.mark.asyncio
async def test_dispatcher_dispatch_pending():
    client = MagicMock()
    session_factory = MagicMock()
    sess = MagicMock()
    session_factory.return_value.__enter__.return_value = sess

    dispatcher = TemporalCommandDispatcher(client=client, session_factory=session_factory)

    cmd = MagicMock()
    cmd.id = "c1"
    cmd.claim_token = "token1"
    cmd.command_type = "start"
    cmd.command_key = "k1"
    cmd.payload = {}
    cmd.task_id = "t1"
    cmd.superseded_at = None

    with patch("orchestrator.temporal.command_dispatcher.session_scope") as mock_scope:
        mock_scope.return_value.__enter__.return_value = sess
        sess.get.return_value = cmd
        dispatcher._deliver = AsyncMock()
        await dispatcher._dispatch_one("c1", "token1")
        dispatcher._deliver.assert_called_once()


def test_dispatcher_retry_and_non_retryable():
    dispatcher = TemporalCommandDispatcher(client=MagicMock(), session_factory=MagicMock())
    assert dispatcher._is_non_retryable(ValueError("invalid")) is True
    assert dispatcher._is_non_retryable(RuntimeError("transient")) is False

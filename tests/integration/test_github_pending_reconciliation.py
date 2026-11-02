"""Unmatched deliveries remain retryable until worker metadata is persisted."""

import json

import pytest

from db.models import WorkerRun
from repositories import InboundDeliveryRepository, session_scope
from tests.integration import test_github_webhook_integration as support

session_factory = support.session_factory
webhook_client = support.webhook_client


@pytest.mark.parametrize("metadata_late", [False, True])
def test_redelivery_after_metadata_persistence(webhook_client, session_factory, metadata_late):
    client, secret, _ = webhook_client
    payload = {
        "action": "opened",
        "repository": {"full_name": "octocat/code-agent"},
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "B", "ref": "github/issue-42"},
        },
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": support._sign(body, secret),
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "early-pr",
    }
    task_id = None
    if metadata_late:
        _, data = support._post_issue_labeled(client, secret, "issue-first")
        task_id = data["task_id"]
    early = client.post("/github/webhook", content=body, headers=headers)
    assert early.status_code == 200
    assert early.json()["status"] == "pending"
    with session_scope(session_factory) as session:
        pending = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="early-pr"
        )
        assert pending is not None and pending.task_id is None
    if task_id is None:
        _, data = support._post_issue_labeled(client, secret, "issue-later")
        task_id = data["task_id"]
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    reconciled = client.post("/github/webhook", content=body, headers=headers).json()
    assert reconciled["status"] == "synced" and not reconciled["duplicate"]
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["pr_number"] == 105
        assert run.delivery_metadata["head_sha"] == "B"
    duplicate = client.post("/github/webhook", content=body, headers=headers).json()
    assert duplicate["duplicate"] and duplicate["task_id"] == task_id

"""Signed webhook to persisted delivery state and operator snapshot ordering."""

import json

import pytest

from repositories import InboundDeliveryRepository, session_scope
from tests.integration import test_github_webhook_integration as github_support
from tests.integration.test_github_webhook_integration import (
    _create_delivered_worker_run,
    _post_issue_labeled,
    _sign,
)

session_factory = github_support.session_factory
webhook_client = github_support.webhook_client


def _post(client, secret: str, event: str, payload: dict, delivery: str) -> dict:
    body = json.dumps({"repository": {"full_name": "octocat/code-agent"}, **payload}).encode()
    response = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        },
    )
    assert response.status_code == 200
    return response.json()


def _pr_payload(action: str, pr_num: int, sha: str) -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": pr_num,
            "state": "open",
            "head": {"sha": sha, "ref": "github/issue-42"},
        },
    }


def _check_payload(
    kind: str, cid: int, name: str, status: str, conclusion: str, sha: str, pr_num: int
) -> dict:
    return {
        "action": "completed",
        kind: {
            "id": cid,
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "head_sha": sha,
            "head_branch": "github/issue-42",
            "pull_requests": [{"number": pr_num}],
            "check_suite": {"head_branch": "github/issue-42"},
        },
    }


@pytest.mark.parametrize("kind", ["check_run", "check_suite"])
def test_a_b_delayed_a_cannot_regress_operator_snapshot(webhook_client, session_factory, kind):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "ordering-issue")
    task_id = data["task_id"]
    _create_delivered_worker_run(session_factory, task_id)

    for sha in ("A", "B"):
        _post(client, secret, "pull_request", _pr_payload("synchronize", 105, sha), "head-" + sha)
        _post(
            client,
            secret,
            kind,
            _check_payload(kind, 1 if sha == "A" else 2, "unit", "completed", "success", sha, 105),
            "check-" + sha,
        )

    delayed_res = _post(
        client,
        secret,
        kind,
        _check_payload(kind, 1, "unit", "completed", "failure", "A", 105),
        "delayed-A",
    )
    assert delayed_res["status"] == "ignored"

    snapshot = client.get(f"/tasks/{task_id}", headers={"X-Webhook-Token": "a" * 32}).json()
    assert snapshot["delivery_metadata"]["head_sha"] == "B"
    assert snapshot["delivery_metadata"]["ci_status"] == "passed"
    assert snapshot["delivery_metadata"]["ci_failed_jobs"] == []

    timeline = snapshot.get("timeline", [])
    delivery_events = [
        e for e in timeline if e.get("event_type") in ("delivery_completed", "delivery_failed")
    ]
    assert delivery_events
    assert delivery_events[-1]["event_type"] == "delivery_completed"
    assert delivery_events[-1]["payload"].get("head_sha") == "B"
    assert not any(e.get("event_type") == "delivery_failed" for e in timeline)
    assert not any(
        e.get("payload", {}).get("head_sha") == "A" and e.get("event_type") == "delivery_failed"
        for e in timeline
    )

    with session_scope(session_factory) as session:
        inbound = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="delayed-A"
        )
        assert inbound is None


def test_unseen_head_check_redelivery_after_pr_sync(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "unseen-issue")
    task_id = data["task_id"]
    _create_delivered_worker_run(session_factory, task_id)

    # Step 1: Head A is established.
    _post(client, secret, "pull_request", _pr_payload("opened", 201, "sha-A"), "pr-A")

    # Step 2: Check for unseen head B arrives before PR synchronize for head B.
    unseen_payload = _check_payload("check_run", 501, "unit", "completed", "success", "sha-B", 201)
    unseen_res = _post(client, secret, "check_run", unseen_payload, "check-B-1")
    assert unseen_res["status"] == "ignored"

    # Step 3: PR synchronize event arrives setting head B.
    pr_b_res = _post(
        client, secret, "pull_request", _pr_payload("synchronize", 201, "sha-B"), "pr-B"
    )
    assert pr_b_res["status"] == "synced"

    # Step 4: Redelivery of check-B-1 with same delivery ID now succeeds because it was not consumed
    redeliver_res = _post(client, secret, "check_run", unseen_payload, "check-B-1")
    assert redeliver_res["status"] == "synced"
    assert redeliver_res["duplicate"] is False

    snapshot = client.get(f"/tasks/{task_id}", headers={"X-Webhook-Token": "a" * 32}).json()
    assert snapshot["delivery_metadata"]["head_sha"] == "sha-B"
    assert snapshot["delivery_metadata"]["ci_status"] == "passed"
    delivery_events = [
        e
        for e in snapshot.get("timeline", [])
        if e.get("event_type") in ("delivery_completed", "delivery_failed")
    ]
    assert delivery_events[-1]["payload"].get("head_sha") == "sha-B"
    assert delivery_events[-1]["payload"]["check_run"]["id"] == 501

    with session_scope(session_factory) as session:
        inbound = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="check-B-1"
        )
        assert inbound is not None
        assert inbound.task_id == task_id

    # Step 5: A subsequent delivery with check-B-1 is now treated as duplicate.
    dup_res = _post(client, secret, "check_run", unseen_payload, "check-B-1")
    assert dup_res["status"] == "duplicate"
    assert dup_res["duplicate"] is True

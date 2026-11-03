"""Unmatched deliveries remain retryable until worker metadata is persisted."""

import json
from datetime import UTC, datetime

import pytest

from db.enums import WorkerRunStatus, WorkerType
from db.models import Task, WorkerRun
from repositories import InboundDeliveryRepository, WorkerRunRepository, session_scope
from tests.integration import test_github_webhook_integration as support

session_factory = support.session_factory
webhook_client = support.webhook_client


def _post_event(client, secret: str, event: str, payload: dict, delivery: str) -> dict:
    body = json.dumps({"repository": {"full_name": "octocat/code-agent"}, **payload}).encode()
    response = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": support._sign(body, secret),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        },
    )
    assert response.status_code == 200
    return response.json()


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


def test_prior_run_without_delivery_metadata_remains_pending_until_delivered_run_exists(
    webhook_client, session_factory
):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "attempt-issue")
    task_id = data["task_id"]

    # Attempt 1 failed before persisting delivery_metadata:
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        run_repo = WorkerRunRepository(session)
        run_repo.create_for_task(
            task=task,
            worker_type=WorkerType.CODEX,
            started_at=datetime.now(UTC),
            status=WorkerRunStatus.FAILURE,
            delivery_metadata=None,
        )

    payload = {
        "action": "opened",
        "repository": {"full_name": "octocat/code-agent"},
        "pull_request": {
            "number": 106,
            "state": "open",
            "head": {"sha": "B", "ref": "github/issue-42"},
        },
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-Hub-Signature-256": support._sign(body, secret),
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "early-pr-attempt2",
    }

    # Delivery arrives while only failed attempt without delivery_metadata exists -> pending
    early = client.post("/github/webhook", content=body, headers=headers)
    assert early.status_code == 200
    assert early.json()["status"] == "pending"

    with session_scope(session_factory) as session:
        pending = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="early-pr-attempt2"
        )
        assert pending is not None and pending.task_id is None

    # Attempt 2 finishes and persists delivered worker run:
    delivered_run_id = support._create_delivered_worker_run(session_factory, task_id)

    # Redelivery reconciles onto Attempt 2:
    reconciled = client.post("/github/webhook", content=body, headers=headers).json()
    assert reconciled["status"] == "synced" and not reconciled["duplicate"]

    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, delivered_run_id)
        assert run.delivery_metadata["pr_number"] == 106
        assert run.delivery_metadata["head_sha"] == "B"
        attached = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="early-pr-attempt2"
        )
        assert attached is not None and attached.task_id == task_id


def test_pending_pr_redelivery_cannot_regress_newer_pr_and_ci_state(
    webhook_client, session_factory
):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "order-issue")
    task_id = data["task_id"]

    pr_a = {
        "action": "opened",
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "sha-A", "ref": "github/issue-42"},
            "updated_at": "2026-11-02T10:00:00Z",
        },
    }
    pending = _post_event(client, secret, "pull_request", pr_a, "delivery-pr-A")
    assert pending["status"] == "pending"

    support._create_delivered_worker_run(session_factory, task_id)

    pr_b = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "sha-B", "ref": "github/issue-42"},
            "updated_at": "2026-11-02T11:00:00Z",
        },
    }
    sync_b = _post_event(client, secret, "pull_request", pr_b, "delivery-pr-B")
    assert sync_b["status"] == "synced"

    check_b = {
        "action": "completed",
        "check_run": {
            "id": 901,
            "name": "unit",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "sha-B",
            "check_suite": {"head_branch": "github/issue-42"},
            "pull_requests": [{"number": 105}],
        },
    }
    check_res = _post_event(client, secret, "check_run", check_b, "delivery-check-B")
    assert check_res["status"] == "synced"

    # Redelivery of original A event
    redeliver_a = _post_event(client, secret, "pull_request", pr_a, "delivery-pr-A")
    assert redeliver_a["status"] == "ignored"

    snapshot = client.get(f"/tasks/{task_id}", headers={"X-Webhook-Token": "a" * 32}).json()
    assert snapshot["delivery_metadata"]["head_sha"] == "sha-B"
    assert snapshot["delivery_metadata"]["ci_status"] == "passed"
    assert snapshot["delivery_metadata"]["ci_failed_jobs"] == []
    assert len(snapshot["delivery_metadata"]["check_runs"]) == 1

    with session_scope(session_factory) as session:
        inbound = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="delivery-pr-A"
        )
        assert inbound is not None and inbound.task_id == task_id

    dup = _post_event(client, secret, "pull_request", pr_a, "delivery-pr-A")
    assert dup["status"] == "duplicate" and dup["duplicate"] is True


def test_newer_pending_pr_head_is_applied_and_force_push_can_restore_prior_head(
    webhook_client, session_factory
):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "force-push-issue")
    task_id = data["task_id"]
    pending_b = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "sha-B", "ref": "github/issue-42"},
            "updated_at": "2026-11-02T11:00:00Z",
        },
    }
    assert _post_event(client, secret, "pull_request", pending_b, "pending-pr-B")["status"] == (
        "pending"
    )
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {
            **run.delivery_metadata,
            **{
                "head_sha": "sha-A",
                "head_history": ["sha-A"],
                "pr_updated_at": "2026-11-02T10:00:00Z",
            },
        }

    assert _post_event(client, secret, "pull_request", pending_b, "pending-pr-B")["status"] == (
        "synced"
    )
    pr_a_again = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "sha-A", "ref": "github/issue-42"},
            "updated_at": "2026-11-02T12:00:00Z",
        },
    }
    assert (
        _post_event(client, secret, "pull_request", pr_a_again, "force-push-pr-A")["status"]
        == "synced"
    )

    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["head_sha"] == "sha-A"
        assert run.delivery_metadata["pr_updated_at"] == "2026-11-02T12:00:00Z"


def test_pending_check_for_future_head_stays_retryable(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "future-check-issue")
    task_id = data["task_id"]
    check_b = {
        "action": "completed",
        "check_run": {
            "id": 902,
            "name": "unit",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "sha-B",
            "check_suite": {"head_branch": "github/issue-42"},
            "pull_requests": [{"number": 105}],
        },
    }
    assert _post_event(client, secret, "check_run", check_b, "pending-check-B")["status"] == (
        "pending"
    )
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {
            **run.delivery_metadata,
            "pr_number": 105,
            "head_sha": "sha-A",
            "head_history": ["sha-A"],
        }

    assert _post_event(client, secret, "check_run", check_b, "pending-check-B")["status"] == (
        "ignored"
    )
    with session_scope(session_factory) as session:
        pending = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id="pending-check-B"
        )
        assert pending is not None and pending.task_id is None

    pr_b = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "state": "open",
            "head": {"sha": "sha-B", "ref": "github/issue-42"},
            "updated_at": "2026-11-02T11:00:00Z",
        },
    }
    assert _post_event(client, secret, "pull_request", pr_b, "pr-B-after-check")["status"] == (
        "synced"
    )
    assert _post_event(client, secret, "check_run", check_b, "pending-check-B")["status"] == (
        "synced"
    )
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["head_sha"] == "sha-B"
        assert run.delivery_metadata["ci_status"] == "passed"

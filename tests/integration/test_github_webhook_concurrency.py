"""GitHub verification must not block unrelated webhook requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from db.models import WorkerRun
from repositories import session_scope
from tests.integration import test_github_webhook_integration as support
from tests.integration.test_github_pending_reconciliation import _post_event

session_factory = support.session_factory
webhook_client = support.webhook_client


def test_pending_verification_does_not_block_ping(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "concurrency-issue")
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "head": {"sha": "B", "ref": "github/issue-42"},
        },
    }
    assert (
        _post_event(client, secret, "pull_request", payload, "concurrency-pr")["status"]
        == "pending"
    )
    run_id = support._create_delivered_worker_run(session_factory, data["task_id"])
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {**run.delivery_metadata, "pr_number": 105, "head_sha": "A"}

    entered, release = Event(), Event()

    def slow_fetch(*args):
        entered.set()
        assert release.wait(5)
        return payload["pull_request"]

    with patch(
        "orchestrator.github_delivery_reconciliation._fetch_current_pull_request", slow_fetch
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(
                _post_event, client, secret, "pull_request", payload, "concurrency-pr"
            )
            try:
                assert entered.wait(3)
                ping = pool.submit(_post_event, client, secret, "ping", {}, "concurrency-ping")
                assert ping.result(timeout=2)["status"] == "accepted"
            finally:
                release.set()
            assert pending.result(timeout=3)["status"] == "synced"


def test_persistent_lock_contention_returns_failure(webhook_client, monkeypatch):
    client, secret, _ = webhook_client
    monkeypatch.setattr("apps.api.routes.github.DELIVERY_LOCK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr("apps.api.routes.github.DELIVERY_LOCK_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        "orchestrator.github_delivery_sync._lock_delivery_updates", lambda *_: False
    )
    body = json.dumps({"repository": {"full_name": "octocat/code-agent"}}).encode()
    response = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": support._sign(body, secret),
            "X-GitHub-Event": "check_run",
            "X-GitHub-Delivery": "busy-check",
        },
    )
    assert response.status_code == 503
    assert "redeliver" in response.json()["detail"]

"""Signed webhook to persisted delivery state and operator snapshot ordering."""

import json

import pytest

from tests.integration import test_github_webhook_integration as github_support
from tests.integration.test_github_webhook_integration import (
    _create_delivered_worker_run,
    _post_issue_labeled,
    _sign,
)

session_factory = github_support.session_factory
webhook_client = github_support.webhook_client


@pytest.mark.parametrize("kind", ["check_run", "check_suite"])
def test_a_b_delayed_a_cannot_regress_operator_snapshot(webhook_client, session_factory, kind):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "ordering-issue")
    task_id = data["task_id"]
    _create_delivered_worker_run(session_factory, task_id)

    def post(event, payload, delivery):
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

    for sha in ("A", "B"):
        post(
            "pull_request",
            {
                "action": "synchronize",
                "pull_request": {
                    "number": 105,
                    "state": "open",
                    "head": {"sha": sha, "ref": "github/issue-42"},
                },
            },
            "head-" + sha,
        )
        post(
            kind,
            {
                "action": "completed",
                kind: {
                    "id": 1 if sha == "A" else 2,
                    "name": "unit",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": sha,
                    "head_branch": "github/issue-42",
                    "pull_requests": [{"number": 105}],
                    "check_suite": {"head_branch": "github/issue-42"},
                },
            },
            "check-" + sha,
        )
    post(
        kind,
        {
            "action": "completed",
            kind: {
                "id": 1,
                "name": "unit",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "A",
                "pull_requests": [{"number": 105}],
            },
        },
        "delayed-A",
    )
    snapshot = client.get(f"/tasks/{task_id}", headers={"X-Webhook-Token": "a" * 32}).json()
    assert snapshot["delivery_metadata"]["head_sha"] == "B"
    assert snapshot["delivery_metadata"]["ci_status"] == "passed"
    assert snapshot["delivery_metadata"]["ci_failed_jobs"] == []

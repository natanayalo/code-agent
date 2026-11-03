"""Pending private PR reconciliation uses only authorized task credentials."""

import json
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from db.models import Task, WorkerRun
from repositories import session_scope
from sandbox.ephemeral_store_postgres import PostgresEphemeralSecretStore
from sandbox.secrets import SecretExposurePolicy, SecretScope
from tests.integration import test_github_webhook_integration as support
from tests.integration.test_github_pending_reconciliation import _post_event

session_factory = support.session_factory
webhook_client = support.webhook_client


@pytest.mark.parametrize(
    "credential",
    ["task", "broker", "environment", "wrong_scope", "wrong_policy", "wrong_task", "expired"],
)
def test_private_pr_reconciliation_uses_task_scoped_credential(
    webhook_client, session_factory, monkeypatch, credential
):
    monkeypatch.setenv("CODE_AGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, f"credential-issue-{credential}")
    task_id = data["task_id"]
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "head": {"sha": "B", "ref": "github/issue-42"},
        },
    }
    delivery_id = f"credential-pr-{credential}"
    assert _post_event(client, secret, "pull_request", payload, delivery_id)["status"] == "pending"
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    token = "synthetic-private-pr-token"
    if credential == "environment":
        monkeypatch.setenv("GH_TOKEN", token)
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {**run.delivery_metadata, "pr_number": 105, "head_sha": "A"}
        if credential == "task":
            task.secrets = {"GH_TOKEN": token}
        elif credential != "environment":
            handle = "ephem_private_github"
            task.secret_refs = [{"name": handle}]
            store = PostgresEphemeralSecretStore(session)
            store.store(
                handle,
                token,
                task_id="different-task" if credential == "wrong_task" else task_id,
                scope=SecretScope.CUSTOM if credential == "wrong_scope" else SecretScope.GIT_PUSH,
                exposure_policy=(
                    SecretExposurePolicy.SANDBOX_ENV
                    if credential == "wrong_policy"
                    else SecretExposurePolicy.BROKER_ONLY
                ),
                ttl_seconds=-1 if credential == "expired" else 3600,
            )

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload["pull_request"]).encode()
    allowed = credential in {"task", "broker", "environment"}

    def fetch(request, **kwargs):
        assert request.get_header("Authorization") == (f"Bearer {token}" if allowed else None)
        if not allowed:
            raise OSError("private repository unavailable")
        return response

    with patch("orchestrator.github_delivery_reconciliation.urlopen", side_effect=fetch):
        result = _post_event(client, secret, "pull_request", payload, delivery_id)
    assert result["status"] == ("synced" if allowed else "pending")
    assert token not in json.dumps(result)
    with session_scope(session_factory) as session:
        assert session.get(WorkerRun, run_id).delivery_metadata["head_sha"] == (
            "B" if allowed else "A"
        )


@pytest.mark.parametrize("credential", ["task", "broker"])
@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_private_pr_retries_api_credential_only_after_access_failure(
    webhook_client, session_factory, monkeypatch, caplog, credential, status
):
    from urllib.error import HTTPError

    monkeypatch.setenv("CODE_AGENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GH_TOKEN", "fallback-token")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, f"fallback-issue-{credential}-{status}")
    task_id = data["task_id"]
    payload = {
        "action": "synchronize",
        "pull_request": {"number": 105, "head": {"sha": "B", "ref": "github/issue-42"}},
    }
    delivery_id = f"fallback-pr-{credential}-{status}"
    assert _post_event(client, secret, "pull_request", payload, delivery_id)["status"] == "pending"
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {**run.delivery_metadata, "pr_number": 105, "head_sha": "A"}
        if credential == "task":
            task.secrets = {"GH_TOKEN": "rejected-token"}
        else:
            task.secret_refs = [{"name": "ephem_private_github"}]
            PostgresEphemeralSecretStore(session).store(
                "ephem_private_github",
                "rejected-token",
                task_id=task_id,
                scope=SecretScope.GIT_PUSH,
                exposure_policy=SecretExposurePolicy.BROKER_ONLY,
            )

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload["pull_request"]).encode()
    seen = []

    def fetch(request, **kwargs):
        authorization = request.get_header("Authorization")
        seen.append(authorization)
        if authorization == "Bearer rejected-token":
            raise HTTPError(request.full_url, status, "access denied", {}, None)
        assert authorization == "Bearer fallback-token"
        return response

    with patch("orchestrator.github_delivery_reconciliation.urlopen", side_effect=fetch):
        result = _post_event(client, secret, "pull_request", payload, delivery_id)
    assert result["status"] == ("pending" if status == 500 else "synced")
    assert seen == (
        ["Bearer rejected-token"]
        if status == 500
        else ["Bearer rejected-token", "Bearer fallback-token"]
    )
    assert "rejected-token" not in caplog.text and "fallback-token" not in caplog.text

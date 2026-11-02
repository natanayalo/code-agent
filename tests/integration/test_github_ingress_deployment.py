"""Compose wiring and HTTP rejection for signed GitHub ingress."""

from pathlib import Path

import yaml

from tests.integration import test_github_webhook_integration as github_support
from tests.integration.test_github_webhook_integration import (
    _post_issue_labeled,
)

session_factory = github_support.session_factory
webhook_client = github_support.webhook_client


def test_compose_passes_github_configuration():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    environment = compose["services"]["api"]["environment"]
    for key in (
        "CODE_AGENT_GITHUB_WEBHOOK_SECRET",
        "CODE_AGENT_GITHUB_OWNER_LOGIN",
        "CODE_AGENT_GITHUB_ISSUE_LABEL",
    ):
        assert environment[key].startswith("${" + key + ":-")


def test_signed_issue_intake_and_oversized_http_rejection(webhook_client):
    client, secret, _ = webhook_client
    status, data = _post_issue_labeled(client, secret, "bounded-intake")
    assert status == 200 and data["task_id"]
    response = client.post(
        "/github/webhook",
        content=b"x" * (1024 * 1024 + 1),
        headers={"X-Hub-Signature-256": "sha256=synthetic"},
    )
    assert response.status_code == 413

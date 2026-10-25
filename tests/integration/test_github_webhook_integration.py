"""Integration tests for GitHub webhook intake and delivery synchronization (M30)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.config import SystemConfig
from apps.api.main import create_app
from db.base import Base
from db.enums import TimelineEventType, WorkerRunStatus, WorkerType
from db.models import Task, WorkerRun
from orchestrator import OrchestratorState, TaskRequest, WorkerResult
from orchestrator.execution import TaskExecutionService
from orchestrator.nodes.delivery import _delivery_completed_response
from orchestrator.state import TaskSpec
from repositories import (
    InboundDeliveryRepository,
    TaskTimelineRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class StaticWorker(Worker):
    """Worker double that returns a successful result."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            status="success",
            summary="Worker completed.",
            budget_usage={},
            commands_run=[],
            files_changed=["main.py"],
            artifacts=[],
        )


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def webhook_client(session_factory) -> Iterator[tuple[TestClient, str, SystemConfig]]:
    secret = "test-webhook-secret"
    auth_config = ApiAuthConfig(
        shared_secret="a" * 32,  # gitleaks:allow
        github_webhook_secret=secret,
    )
    system_config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        allowed_repos={"code-agent": "https://github.com/octocat/code-agent.git"},
        github_owner_login="octocat",
        github_issue_label="code-agent",
    )
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=StaticWorker(),
    )
    app = create_app(task_service=task_service, auth_config=auth_config)
    with TestClient(app) as client:
        client.app.state.system_config = system_config
        yield client, secret, system_config


def _post_issue_labeled(
    client: TestClient, secret: str, delivery_id: str, issue_num: int = 42
) -> tuple[int, dict]:
    payload = {
        "action": "labeled",
        "repository": {
            "full_name": "octocat/code-agent",
            "html_url": "https://github.com/octocat/code-agent",
            "owner": {"login": "octocat"},
        },
        "sender": {"login": "octocat"},
        "issue": {
            "number": issue_num,
            "id": 10000 + issue_num,
            "node_id": f"I_kwDO{issue_num}",
            "html_url": f"https://github.com/octocat/code-agent/issues/{issue_num}",
            "title": "Fix memory leak in parser",
            "body": "Parser allocates unbounded memory.",
        },
        "label": {"name": "code-agent"},
    }
    body = json.dumps(payload).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret),
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
        },
    )
    return resp.status_code, resp.json()


def _create_delivered_worker_run(session_factory, task_id: str) -> str:
    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        now = datetime.now(UTC)
        run_repo = WorkerRunRepository(session)
        worker_run = run_repo.create_for_task(
            task=task,
            worker_type=WorkerType.CODEX,
            started_at=now,
            status=WorkerRunStatus.SUCCESS,
            delivery_metadata={
                "delivery_mode": "draft_pr",
                "branch_name": "github/issue-42",
                "source_issue_number": 42,
                "source_issue_url": "https://github.com/octocat/code-agent/issues/42",
            },
        )
        return worker_run.id


def test_github_issue_intake_creates_task_and_deduplicates(webhook_client, session_factory):
    client, secret, _ = webhook_client
    status_code, data = _post_issue_labeled(client, secret, "del-issue-1", issue_num=42)
    assert status_code == 200
    assert data["ok"] is True
    assert data["status"] == "accepted"
    task_id = data["task_id"]

    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        assert task.repo_url == "https://github.com/octocat/code-agent.git"
        assert task.session.channel == "github"
        assert task.session.external_thread_id == "github:octocat/code-agent#issue:42"
        assert task.constraints["github"]["issue_number"] == 42
        assert task.constraints["delivery_mode"] == "draft_pr"
        assert task.constraints["delivery_branch"] == "github/issue-42"

        timeline = TaskTimelineRepository(session).list_by_task(task_id)
        assert len(timeline) >= 1
        assert timeline[0].event_type == TimelineEventType.TASK_INGESTED
        assert timeline[0].event_key == "github_delivery:del-issue-1"

    # Deduplicated delivery
    status_dupe, data_dupe = _post_issue_labeled(client, secret, "del-issue-1", issue_num=42)
    assert status_dupe == 200
    assert data_dupe["ok"] is True
    assert data_dupe["duplicate"] is True
    assert data_dupe["task_id"] == task_id


def test_github_delivery_node_source_issue_propagation(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "del-issue-source", issue_num=42)
    task_id = data["task_id"]

    with session_scope(session_factory) as session:
        task = session.get(Task, task_id)
        state = OrchestratorState(
            task=TaskRequest(
                task_id=task.id,
                task_text=task.task_text,
                repo_url=task.repo_url,
                constraints=task.constraints,
            ),
            task_spec=TaskSpec(
                goal="fix bug",
                delivery_mode="draft_pr",
                delivery_branch="github/issue-42",
            ),
        )
        completed_res = _delivery_completed_response(
            state,
            branch_name="github/issue-42",
            pr_title="Fix memory leak in parser (#42)",
            merged_result=WorkerResult(status="success", summary="done"),
        )
        assert completed_res["timeline_events"][0].payload["source_issue_number"] == 42
        assert (
            completed_res["timeline_events"][0].payload["source_issue_url"]
            == "https://github.com/octocat/code-agent/issues/42"
        )


def test_github_pull_request_delivery_synchronization(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "del-issue-pr-test", issue_num=42)
    task_id = data["task_id"]
    run_id = _create_delivered_worker_run(session_factory, task_id)

    delivery_pr = "del-pr-105"
    pr_payload = {
        "action": "opened",
        "repository": {"full_name": "octocat/code-agent"},
        "pull_request": {
            "number": 105,
            "html_url": "https://github.com/octocat/code-agent/pull/105",
            "node_id": "PR_kwDO105",
            "state": "open",
            "draft": True,
            "merged": False,
            "head": {"sha": "sha-pr-105", "ref": "github/issue-42"},
            "base": {"ref": "master"},
        },
    }
    body = json.dumps(pr_payload).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_pr,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "synced"

    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["pr_number"] == 105
        assert run.delivery_metadata["pr_url"] == "https://github.com/octocat/code-agent/pull/105"
        assert run.delivery_metadata["head_sha"] == "sha-pr-105"

        inbound = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="github", delivery_id=delivery_pr
        )
        assert inbound is not None
        assert inbound.task_id == task_id


def test_github_check_run_sync_and_snapshot_inspection(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = _post_issue_labeled(client, secret, "del-issue-ci-test", issue_num=42)
    task_id = data["task_id"]
    run_id = _create_delivered_worker_run(session_factory, task_id)

    # 1. Post failing check run
    check_payload = {
        "action": "completed",
        "repository": {"full_name": "octocat/code-agent"},
        "check_run": {
            "id": 9001,
            "name": "unit-tests",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/octocat/code-agent/runs/9001",
            "head_sha": "sha-pr-105",
            "check_suite": {
                "head_branch": "github/issue-42",
                "pull_requests": [{"number": 105}],
            },
        },
    }
    body = json.dumps(check_payload).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": _sign(body, secret),
            "X-GitHub-Event": "check_run",
            "X-GitHub-Delivery": "del-check-fail",
        },
    )
    assert resp.status_code == 200

    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["ci_status"] == "failed"
        assert run.delivery_metadata["ci_failed_jobs"] == ["unit-tests"]

    # 2. Post passing re-run
    check_payload["check_run"]["conclusion"] = "success"
    body_pass = json.dumps(check_payload).encode()
    resp_pass = client.post(
        "/github/webhook",
        content=body_pass,
        headers={
            "X-Hub-Signature-256": _sign(body_pass, secret),
            "X-GitHub-Event": "check_run",
            "X-GitHub-Delivery": "del-check-pass",
        },
    )
    assert resp_pass.status_code == 200

    # 3. Query task snapshot via API
    resp_task = client.get(
        f"/tasks/{task_id}",
        headers={"X-Webhook-Token": "a" * 32},  # gitleaks:allow
    )
    assert resp_task.status_code == 200
    snapshot = resp_task.json()
    assert snapshot["task_id"] == task_id
    assert snapshot["constraints"]["github"]["issue_number"] == 42
    assert snapshot["latest_run"]["delivery_metadata"]["ci_status"] == "passed"
    assert snapshot["latest_run"]["delivery_metadata"]["source_issue_number"] == 42

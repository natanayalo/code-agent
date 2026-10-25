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

    # 2. Post passing re-run with a new check run ID (as GitHub Actions emits on re-runs)
    check_payload["check_run"]["id"] = 9002
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

    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        assert run.delivery_metadata["ci_status"] == "passed"
        assert run.delivery_metadata["ci_failed_jobs"] == []
        assert len(run.delivery_metadata["check_runs"]) == 1
        assert run.delivery_metadata["check_runs"][0]["id"] == 9002

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
    assert snapshot["delivery_metadata"]["ci_status"] == "passed"
    assert snapshot["delivery_metadata"]["source_issue_number"] == 42


def test_task_summary_snapshot_projects_delivery_metadata(webhook_client, session_factory):
    """GET /tasks must project delivery_metadata including source issue and CI status."""
    client, _, _ = webhook_client
    _, data = _post_issue_labeled(
        client, "test-webhook-secret", "del-issue-summary-test", issue_num=55
    )
    task_id = data["task_id"]
    _create_delivered_worker_run(session_factory, task_id)

    resp_list = client.get(
        "/tasks",
        headers={"X-Webhook-Token": "a" * 32},  # gitleaks:allow
    )
    assert resp_list.status_code == 200
    matching = [t for t in resp_list.json() if t["task_id"] == task_id]
    assert len(matching) == 1
    assert matching[0]["delivery_metadata"] is not None
    assert matching[0]["delivery_metadata"]["source_issue_number"] == 42
    assert matching[0]["delivery_metadata"]["branch_name"] == "github/issue-42"


def _post_issue_comment(
    client: TestClient, secret: str, delivery_id: str, body: str, issue_num: int = 42
) -> tuple[int, dict]:
    payload = {
        "action": "created",
        "repository": {
            "full_name": "octocat/code-agent",
            "html_url": "https://github.com/octocat/code-agent",
            "owner": {"login": "octocat"},
        },
        "issue": {
            "number": issue_num,
            "title": f"Issue #{issue_num}",
            "body": "Initial issue description",
            "html_url": f"https://github.com/octocat/code-agent/issues/{issue_num}",
        },
        "comment": {
            "id": 5555,
            "body": body,
            "html_url": f"https://github.com/octocat/code-agent/issues/{issue_num}#issuecomment-5555",
            "author_association": "OWNER",
        },
        "sender": {"login": "octocat"},
    }
    raw = json.dumps(payload).encode()
    resp = client.post(
        "/github/webhook",
        content=raw,
        headers={
            "X-Hub-Signature-256": _sign(raw, secret),
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": delivery_id,
        },
    )
    return resp.status_code, resp.json()


def test_github_issue_replay_rejected_when_source_task_pending(webhook_client, session_factory):
    client, secret, _ = webhook_client
    status, _ = _post_issue_labeled(client, secret, "del-issue-replay-init", issue_num=77)
    assert status == 200

    # Source task is still pending -> /code-agent replay must reject as not_replayable
    replay_status, replay_data = _post_issue_comment(
        client, secret, "del-replay-pending", "/code-agent replay", issue_num=77
    )
    assert replay_status == 200
    assert replay_data["ok"] is False
    assert replay_data["status"] == "not_replayable"
    assert "cannot be replayed" in replay_data["detail"]


def test_github_issue_replay_success_and_idempotency(webhook_client, session_factory):
    client, secret, _ = webhook_client
    status, data = _post_issue_labeled(
        client, secret, "del-issue-replay-terminal-init", issue_num=88
    )
    assert status == 200
    source_task_id = data["task_id"]

    # Mark source task completed so it is replayable
    with session_scope(session_factory) as session:
        task = session.get(Task, source_task_id)
        assert task is not None
        task.status = "completed"

    # Replay via /code-agent replay
    replay_status, replay_data = _post_issue_comment(
        client, secret, "del-replay-execute-1", "/code-agent replay", issue_num=88
    )
    assert replay_status == 200
    assert replay_data["ok"] is True
    assert replay_data["status"] == "accepted"
    new_task_id = replay_data["task_id"]
    assert new_task_id is not None
    assert new_task_id != source_task_id

    # Verify task constraints, lineage, and timeline event
    with session_scope(session_factory) as session:
        new_task = session.get(Task, new_task_id)
        assert new_task is not None
        assert new_task.constraints["replayed_from"] == [source_task_id]

        timeline = TaskTimelineRepository(session).list_by_task(new_task_id)
        assert len(timeline) >= 1
        ingested_event = timeline[0]
        assert ingested_event.event_type == TimelineEventType.TASK_INGESTED
        assert ingested_event.event_key == "github_delivery:del-replay-execute-1"
        assert ingested_event.payload["replayed_from"] == source_task_id
        assert ingested_event.payload["issue_number"] == 88

    # Deduplicated delivery with same delivery_id
    dupe_status, dupe_data = _post_issue_comment(
        client, secret, "del-replay-execute-1", "/code-agent replay", issue_num=88
    )
    assert dupe_status == 200
    assert dupe_data["ok"] is True
    assert dupe_data["duplicate"] is True
    assert dupe_data["task_id"] == new_task_id

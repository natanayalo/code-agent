"""PostgreSQL delivery contention must not consume the shared connection pool."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from db.models import WorkerRun
from orchestrator import github_delivery_sync
from repositories import create_session_factory, session_scope
from tests.integration import test_github_webhook_integration as support
from tests.integration.test_github_pending_reconciliation import _post_event

webhook_client = support.webhook_client


@pytest.fixture
def session_factory(monkeypatch):
    url = os.environ.get("CODE_AGENT_DISPOSABLE_POSTGRES_URL")
    if not url:
        pytest.skip("requires a dedicated disposable PostgreSQL database")
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url, pool_size=2, max_overflow=0, pool_timeout=2)
    yield create_session_factory(engine)
    engine.dispose()


def test_busy_deliveries_leave_pool_available_and_complete(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "pool-issue")
    task_id = data["task_id"]
    payload = {
        "action": "synchronize",
        "pull_request": {
            "number": 105,
            "head": {"sha": "B", "ref": "github/issue-42"},
        },
    }
    assert _post_event(client, secret, "pull_request", payload, "pool-pr")["status"] == "pending"
    run_id = support._create_delivered_worker_run(session_factory, task_id)
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {**run.delivery_metadata, "pr_number": 105, "head_sha": "A"}

    entered, release, contention = Event(), Event(), Event()

    def slow_fetch(*args):
        entered.set()
        assert release.wait(15)
        return payload["pull_request"]

    def check(number):
        return {
            "action": "completed",
            "check_run": {
                "id": 991 + number,
                "name": f"ci-{number}",
                "status": "completed",
                "conclusion": "failure" if number == 0 else "success",
                "head_sha": "B",
                "check_suite": {"pull_requests": [{"number": 105}]},
            },
        }

    original_lock = github_delivery_sync._lock_delivery_updates

    def observe_lock(*args):
        acquired = original_lock(*args)
        if not acquired:
            contention.set()
        return acquired

    with (
        patch(
            "orchestrator.github_delivery_reconciliation._fetch_current_pull_request", slow_fetch
        ),
        patch.object(github_delivery_sync, "_lock_delivery_updates", observe_lock),
    ):
        with ThreadPoolExecutor(max_workers=10) as pool:
            first = pool.submit(_post_event, client, secret, "pull_request", payload, "pool-pr")
            try:
                assert entered.wait(5)
                queued = [
                    pool.submit(
                        _post_event, client, secret, "check_run", check(i), f"pool-check-{i}"
                    )
                    for i in range(8)
                ]
                assert contention.wait(4)
                unrelated = pool.submit(
                    client.get, f"/tasks/{task_id}", headers={"X-Webhook-Token": "a" * 32}
                )
                assert unrelated.result(timeout=4).status_code == 200
            finally:
                release.set()
            assert first.result(timeout=5)["status"] == "synced"
            assert all(future.result(timeout=8)["status"] == "synced" for future in queued)
    with session_scope(session_factory) as session:
        meta = session.get(WorkerRun, run_id).delivery_metadata
        assert meta["ci_status"] == "failed"
        assert meta["ci_failed_jobs"] == ["ci-0"]
        assert len(meta["check_runs"]) == 8

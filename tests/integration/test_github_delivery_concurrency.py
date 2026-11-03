"""Concurrent GitHub deliveries preserve every check result."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest

from db.base import Base
from db.models import WorkerRun
from orchestrator import github_delivery_sync
from repositories import create_engine_from_url, create_session_factory, session_scope
from tests.integration import test_github_webhook_integration as support
from tests.integration.test_github_pending_reconciliation import _post_event

webhook_client = support.webhook_client


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def test_concurrent_checks_preserve_both_results(webhook_client, session_factory):
    client, secret, _ = webhook_client
    _, data = support._post_issue_labeled(client, secret, "concurrent-issue")
    run_id = support._create_delivered_worker_run(session_factory, data["task_id"])
    with session_scope(session_factory) as session:
        run = session.get(WorkerRun, run_id)
        run.delivery_metadata = {**run.delivery_metadata, "pr_number": 105, "head_sha": "A"}

    def check(check_id, name, conclusion):
        return {
            "action": "completed",
            "check_run": {
                "id": check_id,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": "A",
                "check_suite": {"pull_requests": [{"number": 105}]},
            },
        }

    first_entered, second_entered, release = Event(), Event(), Event()
    original = github_delivery_sync._apply_worker_run_delivery_update

    def observe_update(*args, **kwargs):
        if args[2]["check_run"]["id"] == 1:
            first_entered.set()
            assert release.wait(5)
        else:
            second_entered.set()
        return original(*args, **kwargs)

    with patch.object(github_delivery_sync, "_apply_worker_run_delivery_update", observe_update):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                _post_event, client, secret, "check_run", check(1, "failed", "failure"), "check-1"
            )
            try:
                assert first_entered.wait(3)
                second = pool.submit(
                    _post_event,
                    client,
                    secret,
                    "check_run",
                    check(2, "passed", "success"),
                    "check-2",
                )
                assert not second_entered.wait(0.5)
            finally:
                release.set()
            assert first.result(timeout=5)["status"] == "synced"
            assert second.result(timeout=5)["status"] == "synced"

    with session_scope(session_factory) as session:
        meta = session.get(WorkerRun, run_id).delivery_metadata
        assert {item["name"] for item in meta["check_runs"]} == {"failed", "passed"}
        assert meta["ci_status"] == "failed"
        assert meta["ci_failed_jobs"] == ["failed"]

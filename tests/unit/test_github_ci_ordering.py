"""CI events cannot select a different PR head."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from orchestrator.github_delivery_sync import (
    _apply_worker_run_delivery_update,
    sync_pr_metadata,
    upsert_check_run,
)


@pytest.mark.parametrize("sha", ["A", None])
def test_ci_cannot_regress_current_head(sha):
    meta = {
        "head_sha": "B",
        "ci_status": "passed",
        "ci_failed_jobs": [],
        "check_runs": [{"id": 2, "head_sha": "B", "conclusion": "success"}],
    }
    expected = deepcopy(meta)
    applied = upsert_check_run(
        meta,
        {"id": 1, "name": "old", "head_sha": sha, "conclusion": "failure", "status": "completed"},
    )
    assert applied is False
    assert meta == expected


def test_missing_head_check_rejected_when_head_unset():
    meta = {}
    applied = upsert_check_run(
        meta,
        {"id": 1, "name": "old", "head_sha": None, "conclusion": "failure", "status": "completed"},
    )
    assert applied is False
    assert meta == {}

    sync_pr_metadata(meta, {"number": 42, "head": {"sha": "B"}})
    assert meta["head_sha"] == "B"
    assert meta["check_runs"] == []

    applied = upsert_check_run(
        meta,
        {"id": 2, "name": "unit", "head_sha": "B", "conclusion": "success", "status": "completed"},
    )
    assert applied is True
    assert meta["ci_status"] == "passed"
    assert meta["ci_failed_jobs"] == []


def test_sync_pr_metadata_prunes_missing_and_stale_head_checks():
    meta = {
        "check_runs": [
            {"id": 1, "name": "missing-head", "head_sha": None, "conclusion": "failure"},
            {"id": 2, "name": "old-head", "head_sha": "A", "conclusion": "failure"},
        ]
    }
    sync_pr_metadata(meta, {"number": 42, "head": {"sha": "B"}})
    assert meta["head_sha"] == "B"
    assert meta["check_runs"] == []
    assert meta["ci_status"] == "pending"
    assert meta["ci_failed_jobs"] == []


def test_apply_worker_run_delivery_update_ignored_for_noncurrent_head():
    run = MagicMock()
    run.delivery_metadata = {
        "head_sha": "B",
        "ci_status": "passed",
        "check_runs": [{"id": 2, "head_sha": "B", "conclusion": "success"}],
    }
    entry, applied = _apply_worker_run_delivery_update(
        run,
        "check_run",
        {
            "check_run": {
                "id": 1,
                "name": "old",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "A",
            }
        },
        head_sha="A",
    )
    assert applied is False
    assert entry is not None
    assert entry["head_sha"] == "A"
    assert run.delivery_metadata["head_sha"] == "B"
    assert run.delivery_metadata["ci_status"] == "passed"


def test_authoritative_new_head_invalidates_old_ci():
    meta = {
        "head_sha": "A",
        "ci_status": "failed",
        "ci_failed_jobs": ["old"],
        "check_runs": [{"head_sha": "A", "conclusion": "failure"}],
    }
    sync_pr_metadata(meta, {"number": 42, "head": {"sha": "B"}})
    assert meta["head_sha"] == "B"
    assert meta["ci_status"] == "pending"
    assert meta["ci_failed_jobs"] == []
    assert meta["check_runs"] == []


def test_sync_pr_metadata_ignores_older_updated_at():
    meta = {
        "head_sha": "B",
        "pr_updated_at": "2026-11-02T12:00:00Z",
    }
    pr = {
        "number": 10,
        "head": {"sha": "A"},
        "updated_at": "2026-11-02T11:00:00Z",
    }
    applied = sync_pr_metadata(meta, pr)
    assert applied is False
    assert meta["head_sha"] == "B"


def test_sync_pr_metadata_ignores_pending_superseded_head():
    meta = {
        "head_sha": "B",
        "head_history": ["A", "B"],
    }
    pr = {
        "number": 10,
        "head": {"sha": "A"},
    }
    applied = sync_pr_metadata(meta, pr, is_pending=True)
    assert applied is False
    assert meta["head_sha"] == "B"


def test_newer_pending_pr_event_can_advance_head():
    meta = {"head_sha": "A", "pr_updated_at": "2026-11-02T10:00:00Z"}
    applied = sync_pr_metadata(
        meta,
        {
            "number": 10,
            "head": {"sha": "B"},
            "updated_at": "2026-11-02T11:00:00Z",
        },
        is_pending=True,
    )
    assert applied is True
    assert meta["head_sha"] == "B"
    assert meta["pr_updated_at"] == "2026-11-02T11:00:00Z"


def test_newer_pr_event_can_restore_head_from_history():
    meta = {
        "head_sha": "B",
        "head_history": ["A", "B"],
        "pr_updated_at": "2026-11-02T11:00:00Z",
    }
    applied = sync_pr_metadata(
        meta,
        {
            "number": 10,
            "head": {"sha": "A"},
            "updated_at": "2026-11-02T12:00:00Z",
        },
    )
    assert applied is True
    assert meta["head_sha"] == "A"
    assert meta["pr_updated_at"] == "2026-11-02T12:00:00Z"

"""CI events cannot select a different PR head."""

from copy import deepcopy

import pytest

from orchestrator.github_delivery_sync import sync_pr_metadata, upsert_check_run


@pytest.mark.parametrize("sha", ["A", None])
def test_ci_cannot_regress_current_head(sha):
    meta = {
        "head_sha": "B",
        "ci_status": "passed",
        "ci_failed_jobs": [],
        "check_runs": [{"id": 2, "head_sha": "B", "conclusion": "success"}],
    }
    expected = deepcopy(meta)
    upsert_check_run(
        meta,
        {"id": 1, "name": "old", "head_sha": sha, "conclusion": "failure", "status": "completed"},
    )
    assert meta == expected


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

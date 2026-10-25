"""Unit tests for GitHub webhook event processing logic (M30)."""

from __future__ import annotations

from unittest.mock import MagicMock

from apps.api.config import SystemConfig
from orchestrator.github_events import (
    check_issue_event_eligibility,
    extract_code_agent_command,
    extract_delivery_identifiers,
    is_authorized_operator,
    process_github_webhook_event,
    sync_pr_metadata,
    synthesize_issue_task_text,
    upsert_check_run,
)


def test_extract_code_agent_command():
    # Present with instruction
    has_cmd, inst = extract_code_agent_command("Please fix bug\n/code-agent solve this now")
    assert has_cmd is True
    assert inst == "solve this now"

    # Present without instruction
    has_cmd, inst = extract_code_agent_command("   /code-agent   ")
    assert has_cmd is True
    assert inst == ""

    # Case insensitive
    has_cmd, inst = extract_code_agent_command("/CODE-AGENT do something")
    assert has_cmd is True
    assert inst == "do something"

    # Absent
    has_cmd, inst = extract_code_agent_command("Just a comment")
    assert has_cmd is False
    assert inst == ""


def test_is_authorized_operator():
    # When config_owner_login is set, only that login is authorized
    assert is_authorized_operator(
        sender_login="octocat",
        author_association="NONE",
        repo_owner_login="other",
        config_owner_login="octocat",
    )
    assert not is_authorized_operator(
        sender_login="mallory",
        author_association="OWNER",
        repo_owner_login="mallory",
        config_owner_login="octocat",
    )

    # When config_owner_login is not set, repo owner login is required for issues
    assert is_authorized_operator(
        sender_login="repoowner",
        author_association="NONE",
        repo_owner_login="RepoOwner",
        config_owner_login=None,
        is_comment=False,
    )
    assert not is_authorized_operator(
        sender_login="contributor",
        author_association="MEMBER",
        repo_owner_login="repoowner",
        config_owner_login=None,
        is_comment=False,
    )

    # For comments without config_owner_login, associations OWNER, MEMBER, COLLABORATOR are accepted
    for role in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert is_authorized_operator(
            sender_login="dev",
            author_association=role,
            repo_owner_login="repoowner",
            config_owner_login=None,
            is_comment=True,
        )
    for role in ("CONTRIBUTOR", "NONE", "FIRST_TIME_CONTRIBUTOR"):
        assert not is_authorized_operator(
            sender_login="dev",
            author_association=role,
            repo_owner_login="repoowner",
            config_owner_login=None,
            is_comment=True,
        )


def test_synthesize_issue_task_text():
    text = synthesize_issue_task_text(
        issue_number=42,
        issue_title="Bug in auth",
        issue_body="Auth fails on token expire.",
        instruction="Fix the timeout handling",
    )
    assert "Issue #42: Bug in auth" in text
    assert "Auth fails on token expire." in text
    assert "Instruction:\nFix the timeout handling" in text

    # Truncation bounded to 10,000 characters
    long_body = "x" * 15_000
    bounded = synthesize_issue_task_text(
        issue_number=1,
        issue_title="Long",
        issue_body=long_body,
    )
    assert len(bounded) == 10_000


def test_check_issue_comment_eligibility():
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        github_owner_login="octocat",
        github_issue_label="code-agent",
    )

    # Issue comment on PR issue is ignored
    payload_pr_comment = {
        "issue": {"pull_request": {"url": "https://api.github.com/pulls/1"}},
        "comment": {"body": "/code-agent fix"},
    }
    eligible, reason, _ = check_issue_event_eligibility(
        payload_pr_comment, "issue_comment", "created", config
    )
    assert eligible is False
    assert reason == "pull_request_comment"

    # Issue comment with unhandled action
    eligible, reason, _ = check_issue_event_eligibility(
        {"issue": {}, "comment": {"body": "/code-agent"}}, "issue_comment", "edited", config
    )
    assert eligible is False
    assert "Unhandled" in (reason or "")

    # Issue comment without trigger
    eligible, reason, _ = check_issue_event_eligibility(
        {"issue": {}, "comment": {"body": "Just asking"}}, "issue_comment", "created", config
    )
    assert eligible is False
    assert reason == "no_trigger_command"

    # Issue comment with trigger
    eligible, reason, inst = check_issue_event_eligibility(
        {"issue": {}, "comment": {"body": "/code-agent run tests"}},
        "issue_comment",
        "created",
        config,
    )
    assert eligible is True
    assert inst == "run tests"


def test_check_issues_action_eligibility():
    config = SystemConfig(
        default_image="img",
        workspace_root="/tmp",
        github_owner_login="octocat",
        github_issue_label="code-agent",
    )

    # Issue opened with /code-agent in body
    eligible, _, inst = check_issue_event_eligibility(
        {"issue": {"body": "/code-agent investigate crash"}}, "issues", "opened", config
    )
    assert eligible is True
    assert inst == "investigate crash"

    # Issue opened with label matching code-agent
    eligible, _, _ = check_issue_event_eligibility(
        {"issue": {"body": "no cmd", "labels": [{"name": "code-agent"}]}},
        "issues",
        "opened",
        config,
    )
    assert eligible is True

    # Issue opened without trigger or label
    eligible, reason, _ = check_issue_event_eligibility(
        {"issue": {"body": "normal issue", "labels": [{"name": "bug"}]}}, "issues", "opened", config
    )
    assert eligible is False
    assert reason == "no_matching_trigger"

    # Issue labeled matching label
    eligible, _, _ = check_issue_event_eligibility(
        {"label": {"name": "code-agent"}}, "issues", "labeled", config
    )
    assert eligible is True

    # Issue labeled mismatch
    eligible, reason, _ = check_issue_event_eligibility(
        {"label": {"name": "wontfix"}}, "issues", "labeled", config
    )
    assert eligible is False
    assert reason == "no_matching_trigger"

    # Issue assigned matching owner
    eligible, _, _ = check_issue_event_eligibility(
        {"assignee": {"login": "octocat"}}, "issues", "assigned", config
    )
    assert eligible is True

    # Issue assigned mismatch
    eligible, reason, _ = check_issue_event_eligibility(
        {"assignee": {"login": "other"}}, "issues", "assigned", config
    )
    assert eligible is False
    assert reason == "no_matching_trigger"


def test_upsert_check_run():
    meta: dict = {}
    check1 = {
        "id": 101,
        "name": "lint",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://ci/101",
        "head_sha": "abc",
    }
    upsert_check_run(meta, check1)
    assert meta["ci_status"] == "passed"
    assert meta["ci_failed_jobs"] == []
    assert len(meta["check_runs"]) == 1

    # Add second check in progress -> pending
    check2 = {
        "id": 102,
        "name": "test",
        "status": "in_progress",
        "conclusion": None,
        "html_url": "https://ci/102",
        "head_sha": "abc",
    }
    upsert_check_run(meta, check2)
    assert meta["ci_status"] == "pending"
    assert len(meta["check_runs"]) == 2

    # Second check fails -> failed
    check2_failed = {
        "id": 102,
        "name": "test",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://ci/102",
        "head_sha": "abc",
    }
    upsert_check_run(meta, check2_failed)
    assert meta["ci_status"] == "failed"
    assert meta["ci_failed_jobs"] == ["test"]
    assert len(meta["check_runs"]) == 2

    # Re-run second check succeeds -> passed
    check2_rerun = {
        "id": 102,
        "name": "test",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://ci/102",
        "head_sha": "abc",
    }
    upsert_check_run(meta, check2_rerun)
    assert meta["ci_status"] == "passed"
    assert meta["ci_failed_jobs"] == []


def test_extract_delivery_identifiers():
    # pull_request
    pr_payload = {"pull_request": {"number": 12, "head": {"sha": "h123", "ref": "feat"}}}
    pr_num, sha, branch = extract_delivery_identifiers(pr_payload, "pull_request")
    assert pr_num == 12
    assert sha == "h123"
    assert branch == "feat"

    # check_run
    cr_payload = {
        "check_run": {
            "head_sha": "c123",
            "check_suite": {"head_branch": "feat", "pull_requests": [{"number": 99}]},
        }
    }
    pr_num, sha, branch = extract_delivery_identifiers(cr_payload, "check_run")
    assert pr_num == 99
    assert sha == "c123"
    assert branch == "feat"

    # check_suite
    cs_payload = {
        "check_suite": {
            "head_sha": "s123",
            "head_branch": "main",
            "pull_requests": [{"number": 55}],
        }
    }
    pr_num, sha, branch = extract_delivery_identifiers(cs_payload, "check_suite")
    assert pr_num == 55
    assert sha == "s123"
    assert branch == "main"


def test_sync_pr_metadata():
    meta: dict = {}
    pr_dict = {
        "number": 7,
        "html_url": "https://github.com/org/repo/pull/7",
        "node_id": "PR_kwDO123",
        "state": "open",
        "draft": True,
        "merged": False,
        "head": {"sha": "sha7", "ref": "feat-branch"},
        "base": {"ref": "master"},
    }
    sync_pr_metadata(meta, pr_dict)
    assert meta["pr_number"] == 7
    assert meta["pr_url"] == "https://github.com/org/repo/pull/7"
    assert meta["pr_node_id"] == "PR_kwDO123"
    assert meta["pr_state"] == "open"
    assert meta["pr_draft"] is True
    assert meta["head_sha"] == "sha7"
    assert meta["branch_name"] == "feat-branch"
    assert meta["base_branch"] == "master"

    # When merged is True
    pr_dict["merged"] = True
    sync_pr_metadata(meta, pr_dict)
    assert meta["pr_state"] == "merged"


def test_process_github_webhook_event_ping_and_unsupported():
    config = SystemConfig(default_image="img", workspace_root="/tmp")
    task_service = MagicMock()

    # Ping event
    ping_res = process_github_webhook_event(
        payload={"zen": "Design for failure."},
        event="ping",
        delivery_id="del-ping",
        config=config,
        task_service=task_service,
    )
    assert ping_res.ok is True
    assert ping_res.status == "accepted"
    assert ping_res.detail == "pong"

    # Unsupported event
    fork_res = process_github_webhook_event(
        payload={},
        event="fork",
        delivery_id="del-fork",
        config=config,
        task_service=task_service,
    )
    assert fork_res.ok is True
    assert fork_res.status == "ignored"
    assert "Unhandled" in (fork_res.detail or "")


def test_build_check_entry_and_extract_delivery_identifiers_extra():
    from orchestrator.github_events import build_check_entry

    cs_entry = build_check_entry(
        {
            "check_suite": {
                "id": 1,
                "status": "completed",
                "conclusion": "success",
                "url": "https://cs/1",
            }
        },
        "check_suite",
        "sha-suite",
    )
    assert cs_entry["id"] == 1
    assert cs_entry["name"] == "Check Suite"
    assert cs_entry["head_sha"] == "sha-suite"

    # Unknown event for identifiers
    pr_num, sha, branch = extract_delivery_identifiers({}, "watch")
    assert pr_num is None and sha is None and branch is None


def test_check_issue_event_eligibility_unhandled_actions():
    config = SystemConfig(default_image="img", workspace_root="/tmp")

    # Unhandled issues action (e.g., closed)
    eligible, reason, _ = check_issue_event_eligibility({}, "issues", "closed", config)
    assert eligible is False
    assert "Unhandled issues action: closed" in (reason or "")

    # Unhandled event
    eligible, reason, _ = check_issue_event_eligibility({}, "push", None, config)
    assert eligible is False
    assert "Unhandled event: push" in (reason or "")

    # Assigned when config_owner_login is None
    eligible, reason, _ = check_issue_event_eligibility(
        {"assignee": {"login": "octocat"}}, "issues", "assigned", config
    )
    assert eligible is False
    assert reason == "no_matching_trigger"


def test_delivery_sync_helpers():
    from orchestrator.github_delivery_sync import (
        _apply_worker_run_delivery_update,
        find_matching_delivery_run,
    )

    # _apply_worker_run_delivery_update when worker_run is None returns None
    assert _apply_worker_run_delivery_update(None, "pull_request", {}, None) is None

    # find_matching_delivery_run with invalid repo_url returns (None, None)
    mock_session = MagicMock()
    run, task = find_matching_delivery_run(mock_session, "", 1, None, None)
    assert run is None and task is None


def test_issue_submission_comment_id_attachment():
    from orchestrator.github_issue_intake import _build_issue_submission

    sub, gh_constraints, num = _build_issue_submission(
        payload={
            "issue": {"number": 10, "title": "Bug"},
            "comment": {"id": 501, "body": "/code-agent run"},
        },
        event="issue_comment",
        action="created",
        delivery_id="del-comment",
        repo_url="https://github.com/octocat/code-agent.git",
        sender_login="octocat",
        instruction="run",
    )
    assert num == 10
    assert gh_constraints["comment_id"] == 501


def test_find_matching_delivery_run_matches():
    from orchestrator.github_delivery_sync import find_matching_delivery_run

    mock_session = MagicMock()
    mock_run = MagicMock()
    mock_run.task = MagicMock()
    mock_run.task.repo_url = "https://github.com/octocat/code-agent.git"
    mock_run.delivery_metadata = {"head_sha": "target-sha", "branch_name": "target-branch"}
    mock_session.scalars.return_value = [mock_run]

    # Match by head_sha
    run, _ = find_matching_delivery_run(
        mock_session,
        "https://github.com/octocat/code-agent.git",
        pr_number=None,
        head_sha="target-sha",
        branch_name=None,
    )
    assert run == mock_run

    # Match by branch_name
    run, _ = find_matching_delivery_run(
        mock_session,
        "https://github.com/octocat/code-agent.git",
        pr_number=None,
        head_sha=None,
        branch_name="target-branch",
    )
    assert run == mock_run

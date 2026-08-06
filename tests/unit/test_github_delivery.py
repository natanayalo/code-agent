import json
import subprocess
from unittest.mock import MagicMock, patch

from orchestrator.github_delivery import publish_draft_pr_from_workspace


def test_publish_draft_pr_pushes_without_token_in_command_and_creates_pr(tmp_path) -> None:
    workspace = tmp_path / "workspace-1"
    workspace.mkdir()
    completed = MagicMock(returncode=0)
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    response.read.return_value = json.dumps(
        {
            "html_url": "https://github.com/example/project/pull/7",
            "number": 7,
            "head": {"sha": "abc123"},
        }
    ).encode()

    with (
        patch("orchestrator.github_delivery.default_workspace_root", return_value=tmp_path),
        patch("orchestrator.github_delivery.subprocess.run", return_value=completed) as run_mock,
        patch("orchestrator.github_delivery.urlopen", return_value=response) as open_mock,
    ):
        metadata = publish_draft_pr_from_workspace(
            repo_url="https://github.com/example/project.git",
            workspace_id="workspace-1",
            branch_name="qa/evidence",
            base_branch="master",
            pr_title="Evidence PR",
            pr_body="Do not merge.",
            token="task-token",
        )

    assert metadata == {
        "delivery_mode": "draft_pr",
        "branch_name": "qa/evidence",
        "pr_url": "https://github.com/example/project/pull/7",
        "pr_number": 7,
        "head_sha": "abc123",
    }
    command = run_mock.call_args.args[0]
    assert command[-2:] == [
        "https://github.com/example/project.git",
        "HEAD:refs/heads/qa/evidence",
    ]
    assert "task-token" not in " ".join(command)
    assert "task-token" not in run_mock.call_args.kwargs["env"]["GIT_CONFIG_VALUE_0"]
    request = open_mock.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer task-token"
    assert json.loads(request.data) == {
        "title": "Evidence PR",
        "head": "qa/evidence",
        "base": "master",
        "body": "Do not merge.",
        "draft": True,
    }


def test_publish_draft_pr_stops_when_push_times_out(tmp_path) -> None:
    (tmp_path / "workspace-1").mkdir()
    with (
        patch("orchestrator.github_delivery.default_workspace_root", return_value=tmp_path),
        patch(
            "orchestrator.github_delivery.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 120),
        ),
        patch("orchestrator.github_delivery.urlopen") as open_mock,
    ):
        metadata = publish_draft_pr_from_workspace(
            repo_url="https://github.com/example/project.git",
            workspace_id="workspace-1",
            branch_name="qa/evidence",
            base_branch="master",
            pr_title="Evidence PR",
            pr_body="Do not merge.",
            token="task-token",
        )

    assert metadata is None
    open_mock.assert_not_called()

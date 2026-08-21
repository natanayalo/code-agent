import asyncio
import json
import shutil
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from db.enums import TimelineEventType
from orchestrator.nodes.delivery import (
    _broker_git_environment,
    _capture_delivery_metadata,
    _delivery_files_to_stage,
    _delivery_success_response,
    _merge_delivery_result,
    _reconcile_existing_draft_pr,
    _resolve_broker_github_token,
    _run_deliver_result,
    build_deliver_result_node,
)
from orchestrator.state import OrchestratorState
from sandbox.secrets import (
    EphemeralSecretRecord,
    SecretExposurePolicy,
    SecretScope,
)
from workers.base import WorkerResult


def test_capture_delivery_metadata_uses_task_token_with_github_api() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "repo_url": "https://github.com/example/project.git",
            },
            "task_spec": {"delivery_mode": "draft_pr", "goal": "demo"},
        }
    )
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    response.read.return_value = json.dumps(
        [
            {
                "html_url": "https://github.com/example/project/pull/7",
                "number": 7,
                "head": {"sha": "abc123", "ref": "qa/evidence"},
            }
        ]
    ).encode()

    with patch("orchestrator.github_delivery.urlopen", return_value=response) as open_mock:
        metadata = _capture_delivery_metadata(state, "qa/evidence", "task-token")

    assert metadata == {
        "delivery_mode": "draft_pr",
        "branch_name": "qa/evidence",
        "pr_url": "https://github.com/example/project/pull/7",
        "pr_number": 7,
        "head_sha": "abc123",
    }
    request = open_mock.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer task-token"
    assert "head=example%3Aqa%2Fevidence" in request.full_url


def test_capture_delivery_metadata_retries_github_visibility() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "repo_url": "https://github.com/example/project.git",
            },
            "task_spec": {"delivery_mode": "draft_pr", "goal": "demo"},
        }
    )
    empty_response = MagicMock()
    empty_response.__enter__.return_value = empty_response
    empty_response.__exit__.return_value = None
    empty_response.read.return_value = b"[]"
    visible_response = MagicMock()
    visible_response.__enter__.return_value = visible_response
    visible_response.__exit__.return_value = None
    visible_response.read.return_value = json.dumps(
        [{"html_url": "https://example.test/pr/8", "number": 8, "head": {"sha": "def456"}}]
    ).encode()

    with (
        patch(
            "orchestrator.github_delivery.urlopen",
            side_effect=[empty_response, visible_response],
        ) as open_mock,
        patch("orchestrator.github_delivery.time.sleep") as sleep_mock,
    ):
        metadata = _capture_delivery_metadata(state, "qa/evidence", "task-token")

    assert metadata["pr_url"] == "https://example.test/pr/8"
    assert open_mock.call_count == 2
    sleep_mock.assert_called_once()


def test_broker_git_environment_and_empty_delivery_paths() -> None:
    environment = _broker_git_environment("broker-token")

    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert environment["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")

    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    assert _delivery_files_to_stage(state) == ([], None)
    assert _capture_delivery_metadata(state, "qa/evidence", None) is None
    result = WorkerResult(status="success", summary="delivery complete")
    assert _merge_delivery_result(None, result) is result
    assert build_deliver_result_node(session_factory="factory").keywords == {
        "session_factory": "factory"
    }


@pytest.mark.asyncio
async def test_run_deliver_result_skips_when_preconditions_fail():
    # No result
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    res = await _run_deliver_result(state)
    assert res == {"current_step": "deliver_result"}

    # Not branch or draft_pr mode
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "workspace", "goal": "demo"},
        }
    )
    res = await _run_deliver_result(state)
    assert res == {"current_step": "deliver_result"}

    # No workspace
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "branch", "goal": "demo"},
        }
    )
    res = await _run_deliver_result(state)
    assert res == {"current_step": "deliver_result"}


def _delivery_state(
    *,
    delivery_mode: str = "branch",
    files_changed: list[str] | None = None,
    secret_refs: list[dict[str, str]] | None = None,
) -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "task-123",
                "task_text": "demo",
                "repo_url": "https://github.com/example/project.git",
                "secret_refs": secret_refs or [],
            },
            "result": {
                "status": "success",
                "summary": "implementation complete",
                "files_changed": files_changed or [],
            },
            "task_spec": {
                "delivery_mode": delivery_mode,
                "delivery_branch": "qa/evidence",
                "goal": "demo",
            },
            "dispatch": {"workspace_id": "ws-1", "worker_type": "codex"},
        }
    )


@pytest.fixture
def broker_workspace(monkeypatch, tmp_path):
    root = tmp_path / "workspaces"
    workspace_path = root / "ws-1"
    trusted_git_dir = root / ".code-agent-git" / "ws-1"
    workspace_path.mkdir(parents=True)
    trusted_git_dir.mkdir(parents=True)
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: root)
    return workspace_path, trusted_git_dir


@pytest.mark.asyncio
async def test_run_deliver_result_reports_missing_trusted_workspace():
    state = _delivery_state()

    res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "workspace or trusted git dir missing" in res["result"].summary


@pytest.mark.asyncio
async def test_run_deliver_result_requires_token_for_draft_pr(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    state = _delivery_state(delivery_mode="draft_pr")

    res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "GH_TOKEN" in res["result"].summary


@pytest.mark.asyncio
async def test_run_deliver_result_stages_only_reported_files_and_pushes(
    broker_workspace,
) -> None:
    workspace_path, _ = broker_workspace
    (workspace_path / "coverage_report.txt").write_text("diagnostic", encoding="utf-8")
    (workspace_path / "run_test_select.py").write_text("diagnostic", encoding="utf-8")
    state = _delivery_state(
        files_changed=[
            "src/changed.py",
            "coverage_report.txt",
            "run_test_select.py",
            "run_test_selection.py",
            ".code-agent/native-agent-runner/provider.log",
        ]
    )
    git_commands: list[list[str]] = []

    def _run(command, **_kwargs):
        git_commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1 if command[-3:] == ["diff", "--cached", "--quiet"] else 0,
            "",
            "",
        )

    with (
        patch("subprocess.run", side_effect=_run),
        patch("orchestrator.nodes.delivery.start_optional_span"),
    ):
        res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED
    assert any(command[-3:] == ["add", "--", "src/changed.py"] for command in git_commands)
    assert not any(command[-2:] == ["add", "."] for command in git_commands)
    assert any(
        command[-4:] == ["push", "-u", "origin", "HEAD:refs/heads/qa/evidence"]
        for command in git_commands
    )
    assert all("coverage_report.txt" not in command for command in git_commands)
    assert all("run_test_select.py" not in command for command in git_commands)
    assert all("run_test_selection.py" not in command for command in git_commands)
    assert all(
        ".code-agent/native-agent-runner/provider.log" not in command for command in git_commands
    )


@pytest.mark.asyncio
async def test_run_deliver_result_keeps_event_loop_responsive_during_broker_git(
    broker_workspace,
) -> None:
    """Slow broker Git commands must not stall other Temporal coroutines."""
    state = _delivery_state(files_changed=["src/changed.py"])

    def _slow_run(command, **_kwargs):
        time.sleep(0.05)
        return subprocess.CompletedProcess(
            command,
            1 if command[-3:] == ["diff", "--cached", "--quiet"] else 0,
            "",
            "",
        )

    async def _other_coroutine() -> None:
        await asyncio.sleep(0.01)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with (
        patch("subprocess.run", side_effect=_slow_run),
        patch("orchestrator.nodes.delivery.start_optional_span"),
    ):
        delivery = asyncio.create_task(_run_deliver_result(state))
        await _other_coroutine()
        assert loop.time() - started_at < 0.1
        result = await delivery

    assert result["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED


@pytest.mark.asyncio
async def test_run_deliver_result_commits_with_explicit_broker_identity(
    broker_workspace,
    monkeypatch,
) -> None:
    workspace_path, trusted_git_dir = broker_workspace
    remote_path = workspace_path.parent / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=master", str(remote_path)], check=True
    )
    subprocess.run(["git", "init", "--initial-branch=master", str(workspace_path)], check=True)
    (workspace_path / "src").mkdir()
    changed_file = workspace_path / "src" / "changed.py"
    changed_file.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace_path), "add", "--", "src/changed.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace_path),
            "-c",
            "user.name=Bootstrap",
            "-c",
            "user.email=bootstrap@example.invalid",
            "commit",
            "-m",
            "Initial commit",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace_path), "remote", "add", "origin", str(remote_path)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace_path), "push", "-u", "origin", "master"],
        check=True,
    )
    shutil.copytree(workspace_path / ".git", trusted_git_dir, dirs_exist_ok=True)

    changed_file.write_text("after\n", encoding="utf-8")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    result = await _run_deliver_result(_delivery_state(files_changed=["src/changed.py"]))

    assert result["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED
    author = subprocess.run(
        ["git", "--git-dir", str(remote_path), "log", "--format=%an <%ae>", "-1", "qa/evidence"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert author.stdout.strip() == "Code Agent <code-agent@localhost>"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_at", "failure_message"),
    [
        ("reset", "reset staging area"),
        ("add", "stage files"),
        ("diff", "inspect staged files"),
        ("commit", "commit"),
        ("push", "push branch"),
    ],
)
async def test_run_deliver_result_reports_broker_git_failures(
    broker_workspace,
    failure_at: str,
    failure_message: str,
) -> None:
    state = _delivery_state(files_changed=["src/changed.py"])

    def _run(command, **_kwargs):
        if failure_at == "reset" and command[-1] == "reset":
            return subprocess.CompletedProcess(command, 1, "", "failure")
        if failure_at == "add" and command[-3:] == ["add", "--", "src/changed.py"]:
            return subprocess.CompletedProcess(command, 1, "", "failure")
        if failure_at == "commit" and command[-3] == "commit":
            return subprocess.CompletedProcess(command, 1, "", "failure")
        if failure_at == "push" and command[-4] == "push":
            return subprocess.CompletedProcess(command, 1, "", "failure")
        if failure_at == "diff" and command[-3:] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(command, 2, "", "failure")
        if failure_at == "commit" and command[-3:] == ["diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch("subprocess.run", side_effect=_run),
        patch("orchestrator.nodes.delivery.start_optional_span"),
    ):
        res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert failure_message in res["result"].summary


@pytest.mark.asyncio
async def test_run_deliver_result_rejects_unsafe_reported_path(broker_workspace) -> None:
    state = _delivery_state(files_changed=["../coverage_report.txt"])

    with (
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ),
        patch("orchestrator.nodes.delivery.start_optional_span"),
    ):
        res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "unsafe path" in res["result"].summary


@pytest.mark.asyncio
async def test_run_deliver_result_resolves_broker_only_github_token(monkeypatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    secret_ref_name = "ephem_github_token"
    record = EphemeralSecretRecord(
        handle_id=secret_ref_name,
        task_id="task-123",
        value="broker-token",
        required_scope=SecretScope.GIT_PUSH,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
    )
    store = MagicMock()
    store.get_record.return_value = record
    store.get.return_value = "broker-token"
    metadata = {
        "delivery_mode": "draft_pr",
        "branch_name": "qa/evidence",
        "pr_url": "https://github.com/example/project/pull/7",
    }
    state = _delivery_state(
        delivery_mode="draft_pr",
        secret_refs=[{"name": secret_ref_name}],
    )

    with (
        patch(
            "sandbox.ephemeral_store_postgres.SessionFactoryEphemeralSecretStore",
            return_value=store,
        ),
        patch(
            "orchestrator.nodes.delivery._capture_delivery_metadata",
            return_value=metadata,
        ) as capture_mock,
        patch("orchestrator.nodes.delivery.start_optional_span"),
    ):
        res = await _run_deliver_result(state, session_factory=MagicMock())

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED
    assert capture_mock.call_args.args[2] == "broker-token"
    store.get.assert_called_once_with(secret_ref_name, task_id="task-123")


@pytest.mark.asyncio
async def test_draft_pr_delivery_publishes_or_fails_when_metadata_is_missing() -> None:
    state = _delivery_state(delivery_mode="draft_pr")
    missing_metadata = {"delivery_mode": "draft_pr", "branch_name": "qa/evidence"}
    published_metadata = {
        **missing_metadata,
        "pr_url": "https://github.com/example/project/pull/8",
    }

    with (
        patch(
            "orchestrator.nodes.delivery._capture_delivery_metadata",
            return_value=missing_metadata,
        ),
        patch(
            "orchestrator.nodes.delivery.publish_draft_pr_from_workspace",
            return_value=published_metadata,
        ) as publish_mock,
    ):
        success = await _delivery_success_response(
            state,
            WorkerResult(status="success", summary="delivered"),
            "qa/evidence",
            "Evidence PR",
            "body",
            "broker-token",
        )

    assert success["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED
    assert success["result"].delivery_metadata == published_metadata
    publish_mock.assert_called_once()

    with (
        patch(
            "orchestrator.nodes.delivery._capture_delivery_metadata",
            return_value=missing_metadata,
        ),
        patch(
            "orchestrator.nodes.delivery.publish_draft_pr_from_workspace",
            return_value=None,
        ),
    ):
        failure = await _delivery_success_response(
            state,
            WorkerResult(status="success", summary="delivered"),
            "qa/evidence",
            "Evidence PR",
            "body",
            "broker-token",
        )

    assert failure["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "did not confirm" in failure["result"].summary


@pytest.mark.asyncio
async def test_broker_token_ignores_non_git_push_secret_and_draft_reconcile_without_pr() -> None:
    secret_ref_name = "ephem_custom_token"
    store = MagicMock()
    store.get_record.return_value = EphemeralSecretRecord(
        handle_id=secret_ref_name,
        task_id="task-123",
        value="not-a-github-token",
        required_scope=SecretScope.CUSTOM,
        exposure_policy=SecretExposurePolicy.BROKER_ONLY,
    )
    state = _delivery_state(
        delivery_mode="draft_pr",
        secret_refs=[{"name": secret_ref_name}],
    )

    with patch(
        "sandbox.ephemeral_store_postgres.SessionFactoryEphemeralSecretStore",
        return_value=store,
    ):
        assert _resolve_broker_github_token(state, MagicMock()) is None

    with patch(
        "orchestrator.nodes.delivery._capture_delivery_metadata",
        return_value={"delivery_mode": "draft_pr", "branch_name": "qa/evidence"},
    ):
        reconciled = await _reconcile_existing_draft_pr(
            state,
            branch_name="qa/evidence",
            pr_title="Evidence PR",
            gh_token="broker-token",
        )

    assert reconciled is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch_name",
    [
        "master",
        "main",
        "-unsafe",
        "@",
        "/foo",
        "foo/",
        "foo//bar",
        "foo.",
        "foo..bar",
        "foo/bar.lock",
        "foo/.bar",
        "foo~bar",
        "foo^bar",
        "foo:bar",
        "foo?bar",
        "foo*bar",
        "foo[bar",
        "foo\\bar",
        "foo bar",
        "foo@{bar",
        "foo\rbar",
        "foo\x01bar",
        "foo\x7fbar",
    ],
)
async def test_run_deliver_result_rejects_invalid_branch_names(branch_name: str) -> None:
    state = _delivery_state()
    state.task_spec.delivery_branch = branch_name

    res = await _run_deliver_result(state)

    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    if branch_name in {"master", "main"}:
        assert "protected" in res["timeline_events"][0].message
    else:
        assert "invalid or unsafe" in res["timeline_events"][0].message
    assert res["result"].status == "failure"

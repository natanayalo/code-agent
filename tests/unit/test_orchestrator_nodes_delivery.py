from unittest.mock import AsyncMock, patch

import pytest

from db.enums import TimelineEventType
from orchestrator.nodes.delivery import _build_delivery_prompt, _run_deliver_result
from orchestrator.state import OrchestratorState
from workers.base import WorkerResult
from workers.facade import WorkerFacade


def test_build_delivery_prompt_draft_pr():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_spec": {"delivery_mode": "draft_pr", "goal": "demo"}}
    )
    prompt = _build_delivery_prompt(state, "my-branch", "my-title", "my-body")
    assert "draft PR titled 'my-title'" in prompt
    assert "body 'my-body'" in prompt
    assert "Delivery mode: draft_pr" in prompt
    assert "my-branch" in prompt


def test_build_delivery_prompt_branch():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_spec": {"delivery_mode": "branch", "goal": "demo"}}
    )
    prompt = _build_delivery_prompt(state, "my-branch", "my-title", "my-body")
    assert "draft PR" not in prompt
    assert "Delivery mode: branch" in prompt


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


@pytest.mark.asyncio
async def test_run_deliver_result_missing_worker():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "branch", "goal": "demo"},
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    # Available workers will fall back to gemini, which is not provided as an argument.
    res = await _run_deliver_result(state)
    assert res["current_step"] == "deliver_result"
    assert "timeline_events" in res
    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "no suitable delivery worker configured" in res["timeline_events"][0].message


@pytest.mark.asyncio
async def test_run_deliver_result_missing_gh_token_for_pr(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "draft_pr", "goal": "demo"},
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))
        assert "timeline_events" in res
        assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
        assert "GH_TOKEN" in res["timeline_events"][0].message


@pytest.mark.asyncio
async def test_run_deliver_result_success(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake_token")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "branch", "goal": "demo"},
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()
    worker_mock.run.return_value = WorkerResult(status="success", summary="done")

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
        patch("orchestrator.nodes.delivery.set_current_span_attribute"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))

        assert res["current_step"] == "deliver_result"
        assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_COMPLETED
        assert res["timeline_events"][0].payload["branch"] == "task/None"


@pytest.mark.asyncio
async def test_run_deliver_result_worker_runtime_error(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake_token")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "branch", "goal": "demo"},
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()
    worker_mock.run.side_effect = RuntimeError("failed")

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))

        assert "timeline_events" in res
        assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
        assert "Delivery execution failed" in res["timeline_events"][0].message


@pytest.mark.asyncio
async def test_run_deliver_result_worker_status_failure(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake_token")
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {"delivery_mode": "branch", "goal": "demo"},
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()
    worker_mock.run.return_value = WorkerResult(
        status="failure", summary="bad things", stdout="out", stderr="err"
    )

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
        patch("orchestrator.nodes.delivery.set_current_span_attribute"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))

        assert "timeline_events" in res
        assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
        assert "Delivery script failed" in res["timeline_events"][0].message
        assert res["timeline_events"][0].payload["stdout"] == "out"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch_name",
    [
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
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success", "summary": "ok"},
            "task_spec": {
                "delivery_mode": "branch",
                "goal": "demo",
                "delivery_branch": branch_name,
            },
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=AsyncMock()))
    assert res["timeline_events"][0].event_type == TimelineEventType.DELIVERY_FAILED
    assert "invalid or unsafe" in res["timeline_events"][0].message
    assert res["result"].status == "failure"


@pytest.mark.asyncio
async def test_run_deliver_result_merges_worker_result_on_success() -> None:
    from workers.base import ArtifactReference

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "summary": "impl summary",
                "artifacts": [{"name": "file.py", "uri": "file:///file.py"}],
            },
            "task_spec": {
                "delivery_mode": "branch",
                "goal": "demo",
                "delivery_branch": "valid-branch",
            },
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()
    worker_mock.run.return_value = WorkerResult(
        status="success",
        summary="delivery summary",
        artifacts=[ArtifactReference(name="delivery.log", uri="file:///delivery.log")],
        json_payload={"pr_link": "http"},
    )

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))

        merged_result = res["result"]
        assert merged_result.status == "success"
        assert "impl summary" in merged_result.summary
        assert "Delivery Output:" in merged_result.summary
        assert "delivery summary" in merged_result.summary
        assert len(merged_result.artifacts) == 2
        assert merged_result.artifacts[0].name == "file.py"
        assert merged_result.artifacts[1].name == "delivery.log"
        assert merged_result.json_payload == {"pr_link": "http"}


@pytest.mark.asyncio
async def test_run_deliver_result_merge_omits_missing_summary_text() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "success"},
            "task_spec": {
                "delivery_mode": "branch",
                "goal": "demo",
                "delivery_branch": "valid-branch",
            },
            "dispatch": {"workspace_id": "ws-1", "worker_type": "antigravity"},
        }
    )
    worker_mock = AsyncMock()
    worker_mock.run.return_value = WorkerResult(status="success")

    with (
        patch("orchestrator.nodes.delivery.start_optional_span"),
        patch("orchestrator.nodes.delivery.set_span_input_output"),
    ):
        res = await _run_deliver_result(state, worker=WorkerFacade(antigravity_worker=worker_mock))

    assert res["result"].summary == "Delivery completed."

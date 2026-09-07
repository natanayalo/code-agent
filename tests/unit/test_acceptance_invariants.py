"""Acceptance requires trusted delivery and required checks, not worker prose."""

import pytest

from orchestrator.acceptance import enforce_task_acceptance, task_acceptance_rejection
from orchestrator.nodes.delivery import _run_deliver_result
from orchestrator.state import OrchestratorState


def state_for(mode="draft_pr"):
    return OrchestratorState.model_validate(
        {
            "task": {"task_text": "deliver result"},
            "task_spec": {"goal": "deliver", "delivery_mode": mode},
            "result": {"status": "success", "summary": "done", "files_changed": ["fix.py"]},
            "verification": {"status": "passed"},
            "attempt_count": 2,
        }
    )


@pytest.mark.parametrize("mode", ["branch", "draft_pr"])
def test_untrusted_worker_metadata_cannot_complete_delivery(mode):
    state = state_for(mode)
    state.result.delivery_metadata = {"branch_name": "task/test", "pr_url": "https://example/pr/1"}
    assert task_acceptance_rejection(state)[0] == "incomplete_delivery"
    enforce_task_acceptance(state)
    assert state.result.status == "failure"
    assert state.result.files_changed == ["fix.py"]


@pytest.mark.parametrize("mode", ["branch", "draft_pr"])
def test_current_broker_confirmation_accepts_retry_after_delivery_failure(mode):
    state = state_for(mode)
    state.result.delivery_metadata = {"branch_name": "task/test", "pr_url": "https://example/pr/1"}
    state = state.model_copy(update={"timeline_events": []})
    payload = state.model_dump()
    payload["timeline_events"] = [
        {"event_type": event, "attempt_number": 2, "payload": {"branch": "task/test"}}
        for event in ["delivery_failed", "delivery_completed"]
    ]
    state = OrchestratorState.model_validate(payload)
    assert task_acceptance_rejection(state) is None
    state.timeline_events[-1].attempt_number = 1
    assert task_acceptance_rejection(state)[0] == "incomplete_delivery"


@pytest.mark.parametrize("label", ["independent_verifier", "deterministic_commands"])
@pytest.mark.parametrize("status", ["failed", "warning"])
def test_required_checks_cannot_be_waived_by_read_only_or_summary(label, status):
    state = state_for("summary")
    state.task.constraints["read_only"] = True
    state.task_spec.verification_commands = ["pytest"]
    payload = state.model_dump()
    payload["verification"] = {"status": "warning", "items": [{"label": label, "status": status}]}
    assert task_acceptance_rejection(OrchestratorState.model_validate(payload)) is not None


def test_optional_advisory_warning_does_not_require_artificial_delivery():
    payload = state_for("summary").model_dump()
    payload["verification"] = {
        "status": "warning",
        "items": [{"label": "post_run_lint", "status": "warning"}],
    }
    assert task_acceptance_rejection(OrchestratorState.model_validate(payload)) is None


@pytest.mark.asyncio
async def test_reported_success_without_workspace_fails_and_preserves_evidence():
    state = state_for()
    output = await _run_deliver_result(state)
    assert output["result"].status == "failure"
    assert output["result"].failure_kind == "incomplete_delivery"
    assert output["result"].files_changed == ["fix.py"]
    assert output["timeline_events"][-1].event_type == "delivery_failed"


@pytest.mark.asyncio
async def test_unavailable_verifier_blocks_delivery_before_git(monkeypatch):
    payload = state_for().model_dump()
    payload["verification"] = {
        "status": "warning",
        "items": [
            {"label": "independent_verifier", "status": "warning", "reason_code": "provider_auth"}
        ],
    }
    state = OrchestratorState.model_validate(payload)

    def unexpected(*args):
        pytest.fail("Broker Git must not run before required verification passes")

    monkeypatch.setattr("orchestrator.nodes.delivery._resolve_broker_github_token", unexpected)
    output = await _run_deliver_result(state)
    assert output["result"].failure_kind == "infra_verifier_unavailable"


def test_existing_failure_kind_and_artifacts_are_preserved():
    payload = state_for().model_dump()
    payload["result"].update(status="error", failure_kind="sandbox_infra")
    state = OrchestratorState.model_validate(payload)
    enforce_task_acceptance(state)
    assert state.result.failure_kind == "sandbox_infra"
    assert state.result.files_changed == ["fix.py"]


@pytest.mark.parametrize("mode, rejected", [("draft_pr", True), ("summary", False)])
def test_missing_verification_is_required_for_external_delivery(mode, rejected):
    state = state_for(mode)
    state.verification = None
    assert (task_acceptance_rejection(state) is not None) == rejected


def test_missing_result_is_an_explicit_worker_failure():
    state = state_for("summary")
    state.result = None
    enforce_task_acceptance(state)
    assert state.result.status == "failure"
    assert state.result.failure_kind == "worker_failure"


def test_raw_failed_verification_preserves_failure_kind():
    state = state_for("summary")
    state.verification = {"status": "failed", "failure_kind": "scope_mismatch"}
    assert task_acceptance_rejection(state)[0] == "scope_mismatch"


def test_successful_required_checks_accept_summary_and_enforcement_is_noop():
    payload = state_for("summary").model_dump()
    payload["task_spec"]["verification_commands"] = ["pytest"]
    payload["verification"]["items"] = [
        {"label": label, "status": "passed"}
        for label in ["independent_verifier", "deterministic_commands"]
    ]
    state = OrchestratorState.model_validate(payload)
    original = state.result
    enforce_task_acceptance(state)
    assert state.result is original


@pytest.mark.parametrize(
    "key", ["verification_commands", "operator_post_worker_verification_commands"]
)
def test_operator_required_checks_cannot_be_omitted_from_report(key):
    state = state_for("summary")
    state.task.constraints[key] = ["pytest"]
    assert task_acceptance_rejection(state)[0] == "infra_verifier_unavailable"


def test_read_only_required_command_failure_retains_test_failure_kind():
    from orchestrator.nodes.verification_result import verify_result

    state = state_for("summary")
    state.result.files_changed = []
    state.task.constraints["read_only"] = True
    state.task_spec.verification_commands = ["exit 23"]
    updates = verify_result(state, deterministic_verifier_outcome=("failed", "exit 23"))
    assert updates["verification"]["status"] == "failed"
    assert updates["verification"]["failure_kind"] == "test_regression"


def test_delivery_failure_without_worker_result_preserves_failure_kind():
    from orchestrator.nodes.delivery import _delivery_failure_response

    state = state_for()
    state.result = None
    updates = _delivery_failure_response(state, "Missing output", "Delivery failed")
    assert updates["result"].status == "failure"
    assert updates["result"].failure_kind == "incomplete_delivery"
    assert updates["result"].summary == "Missing output"

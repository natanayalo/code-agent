from unittest.mock import MagicMock

from orchestrator.graph import _route_after_generate_task_spec, generate_task_spec
from orchestrator.state import OrchestratorState, TaskRequest, TaskSpec
from workers import WorkerResult


def test_generate_task_spec_halts_on_policy_violation(monkeypatch):
    """Verify that generate_task_spec halts and returns an error on policy violations."""
    # 1. Create a violating TaskSpec (missing secret hardcode forbidden action)
    violating_spec = TaskSpec(
        goal="test",
        task_type="feature",
        risk_level="low",
        delivery_mode="workspace",
        forbidden_actions=["something_else"],  # Missing "hardcode_secrets"
        requires_permission=False,
    )

    # 2. Mock build_task_spec_for_request to return this violating spec
    mock_build = MagicMock(return_value=violating_spec)
    monkeypatch.setattr("orchestrator.graph.build_task_spec_for_request", mock_build)

    # 3. Create a state
    state = OrchestratorState(
        task=TaskRequest(
            task_id="test-task",
            task_text="test text",
            repo_url="http://github.com/test/test",
            branch="main",
        )
    )

    # 4. Run the node
    response = generate_task_spec(state)

    # 5. Verify it halted
    assert "task_spec_policy:missing_secret_hardcode_forbidden_action" in response["errors"]
    result = response["result"]
    assert isinstance(result, WorkerResult)
    assert result.status == "error"
    assert "safety policy violations" in result.summary
    assert result.next_action_hint == "halt_policy_violation"


def test_route_after_generate_task_spec_with_policy_violation():
    """Verify that the router correctly routes to summarize_result on early gates."""
    # Case: Policy violation error exists
    state = OrchestratorState(
        task=TaskRequest(task_id="t", task_text="t", repo_url="r", branch="b"),
        errors=["task_spec_policy:some_violation"],
    )
    assert _route_after_generate_task_spec(state) == "summarize_result"

    # Case: No policy violation
    state = OrchestratorState(
        task=TaskRequest(task_id="t", task_text="t", repo_url="r", branch="b"), errors=[]
    )
    assert _route_after_generate_task_spec(state) == "load_memory"

    # Case: Clarification-required TaskSpec should halt before worker routing.
    clarification_state = OrchestratorState(
        task=TaskRequest(task_id="t", task_text="fix it", repo_url="r", branch="b"),
        task_spec=TaskSpec(
            goal="fix it",
            task_type="investigation",
            risk_level="low",
            delivery_mode="workspace",
            forbidden_actions=["hardcode_secrets"],
            requires_clarification=True,
            clarification_questions=["What exact failure should be fixed?"],
        ),
    )
    assert _route_after_generate_task_spec(clarification_state) == "summarize_result"


def test_generate_task_spec_halts_on_clarification_requirement(monkeypatch):
    """Clarification-required TaskSpecs should pause before worker dispatch."""
    clarification_spec = TaskSpec(
        goal="fix it",
        task_type="investigation",
        risk_level="low",
        delivery_mode="workspace",
        forbidden_actions=["hardcode_secrets"],
        requires_clarification=True,
        clarification_questions=["What exact failure should be fixed?"],
        requires_permission=False,
    )
    monkeypatch.setattr(
        "orchestrator.graph.build_task_spec_for_request",
        MagicMock(return_value=clarification_spec),
    )

    state = OrchestratorState(
        task=TaskRequest(
            task_id="test-task",
            task_text="fix it",
            repo_url="http://github.com/test/test",
            branch="main",
        )
    )

    response = generate_task_spec(state)

    result = response["result"]
    assert isinstance(result, WorkerResult)
    assert result.status == "failure"
    assert "pending clarification" in (result.summary or "")
    assert result.next_action_hint == "await_manual_follow_up"
    assert "task_spec_requires_clarification" in response["errors"]

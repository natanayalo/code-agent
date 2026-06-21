from orchestrator.graph import (
    _build_worker_request,
    _route_after_await_result,
    transition_to_research_phase,
)
from orchestrator.state import OrchestratorState, ScoutPhaseResult, SessionRef, TaskRequest
from workers import WorkerResult


def test_deep_scout_repo_success_routes_to_research():
    state = OrchestratorState(
        task=TaskRequest(task_text="x", constraints={"scout_mode": "deep"}),
        result=WorkerResult(
            status="success",
            summary="Repo ok",
            next_action_hint="persist_memory",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        ),
    )
    assert _route_after_await_result(state) == "transition_to_research_phase"


def test_deep_scout_repo_failure_skips_research():
    state = OrchestratorState(
        task=TaskRequest(task_text="x", constraints={"scout_mode": "deep"}),
        result=WorkerResult(
            status="failure",
            summary="Repo fail",
            next_action_hint="persist_memory",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        ),
    )
    assert _route_after_await_result(state) == "verify_result"


def test_transition_to_research_phase():
    state = OrchestratorState(
        task=TaskRequest(task_text="x", constraints={"scout_mode": "deep"}),
        result=WorkerResult(
            status="success",
            summary="Repo ok",
            next_action_hint="persist_memory",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        ),
    )
    update = transition_to_research_phase(state)
    assert update["current_step"] == "transition_to_research_phase"
    assert update["scout_phase"] == "research"
    assert len(update["scout_phase_results"]) == 1
    assert update["scout_phase_results"][0]["phase"] == "repo"
    assert update["result"] is None


def test_build_worker_request_injects_phase_metadata():
    state = OrchestratorState(
        session=SessionRef(
            session_id="session1", user_id="u", channel="cli", external_thread_id="t"
        ),
        task=TaskRequest(task_text="x", constraints={"scout_mode": "deep"}),
        scout_phase="research",
    )
    state.scout_phase_results = [
        ScoutPhaseResult(
            phase="repo",
            result=WorkerResult(
                status="success",
                summary="Found the bug in api.py",
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[],
            ),
        )
    ]

    req = _build_worker_request(state)
    assert req.constraints["scout_mode"] == "research"

    # Check that repo summary is injected into memory context
    session_mem = req.memory_context.get("session", {})
    assert session_mem.get("repo_phase_summary") == "Found the bug in api.py"

# ruff: noqa: F403, F405
"""Dispatch and control-flow orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_dispatch_job_preserves_attempt_count():
    """dispatch_job must preserve attempt_count (it is managed externally)."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "codex", "route_reason": "cheap_mechanical_change"},
            "attempt_count": 0,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_preserves_attempt_count_on_retry():
    """attempt_count remains constant throughout a single graph invocation."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "antigravity",
                "route_reason": "verifier_failed_previous_run",
            },
            "attempt_count": 1,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_includes_route_profile_metadata() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )

    result = dispatch_job(state)

    assert result["dispatch"]["worker_type"] == "codex"
    assert result["dispatch"]["worker_profile"] == "codex-native-executor"
    assert result["dispatch"]["runtime_mode"] == "native_agent"


@pytest.mark.anyio
async def test_await_worker_with_timeout_partial_result():
    class SlowWorker(Worker):
        async def run(self, request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return WorkerResult(
                    status="error",
                    summary="partial state flushed",
                    next_action_hint="inspect_workspace_artifacts",
                    commands_run=[{"command": "echo 1"}],
                )
            return WorkerResult(status="success", summary="done")

    worker = SlowWorker()
    res, hint = await _await_worker_with_timeout(
        worker,
        request=WorkerRequest(session_id="test", task_text="test"),
        worker_type="slow",
        session_id="test",
        timeout_seconds=1,
    )

    assert res.status == "error"
    assert res.summary == "partial state flushed"
    assert res.commands_run[0].command == "echo 1"
    assert hint == "worker timed out but yielded partial state after 1s"


@pytest.mark.anyio
async def test_await_worker_with_timeout_stamps_model_execution_on_timeout():
    class HangingWorker(Worker):
        async def run(self, request):
            await asyncio.sleep(10)
            return WorkerResult(status="success", summary="done")

    worker = HangingWorker()
    request = WorkerRequest(
        session_id="test-timeout",
        task_text="test timeout",
        worker_type="codex",
        runtime_mode="native_agent",
        constraints={"model": "gpt-5"},
    )
    res, hint = await _await_worker_with_timeout(
        worker,
        request=request,
        worker_type="codex",
        session_id="test-timeout",
        timeout_seconds=0.01,
    )

    assert res.status == "failure"
    assert res.failure_kind == "timeout"
    assert "timed out after" in hint
    assert res.model_execution is not None
    assert res.model_execution.provider == "codex"
    assert res.model_execution.model == "gpt-5"
    assert res.budget_usage["native_agent"]["model_execution"]["model"] == "gpt-5"


@pytest.mark.anyio
async def test_await_worker_with_timeout_stamps_model_execution_on_crash():
    class CrashingWorker(Worker):
        async def run(self, request):
            raise RuntimeError("Fatal process crash")

    worker = CrashingWorker()
    request = WorkerRequest(
        session_id="test-crash",
        task_text="test crash",
        worker_type="gemini",
        runtime_mode="native_agent",
        constraints={"model": "gemini-2.5-pro"},
    )
    res, hint = await _await_worker_with_timeout(
        worker,
        request=request,
        worker_type="gemini",
        session_id="test-crash",
        timeout_seconds=5,
    )

    assert res.status == "error"
    assert res.failure_kind == "unknown"
    assert hint == "worker crashed unexpectedly"
    assert res.model_execution is not None
    assert res.model_execution.provider == "antigravity"
    assert res.model_execution.model == "gemini-2.5-pro"
    assert res.budget_usage["native_agent"]["model_execution"]["model"] == "gemini-2.5-pro"


def test_dispatch_job_preserves_workspace_id() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "codex",
                "route_reason": "cheap_mechanical_change",
            },
            "dispatch": {"workspace_id": "ws_123"},
        }
    )
    res = dispatch_job(state)
    assert res["dispatch"]["workspace_id"] == "ws_123"


def test_dispatch_job_raises_value_error_if_no_worker() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": None,
                "route_reason": "some_reason",
            },
        }
    )
    with pytest.raises(ValueError, match="choose_worker must set route.chosen_worker"):
        dispatch_job(state)


def test_execution_not_started_does_not_retry_or_reroute() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "codex", "route_reason": "initial"},
            "dispatch": {"worker_type": "codex"},
            "attempt_count": 1,
            "result": {
                "status": "error",
                "failure_kind": "provider_auth",
                "preflight_rejected": False,
                "execution_not_started": True,
            },
        }
    )

    assert _compute_route_escalation(state, frozenset({"codex", "antigravity"})) is None
    prior_worker, retry_strategy = _resolve_brain_retry_context(state)
    assert prior_worker == "codex"
    assert retry_strategy is None


def test_worker_profile_model_and_effort_projected_to_worker_request() -> None:
    """WorkerProfile model and reasoning_effort route through dispatch into WorkerRequest."""
    profile = WorkerProfile(
        name="codex-luna-high",
        worker_type="codex",
        runtime_mode="native_agent",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_id": "task-test-1",
                "task_text": "Implement feature with Luna",
                "worker_profile_override": "codex-luna-high",
            },
        }
    )
    route_decision = _compute_route_decision(
        state,
        available_workers=frozenset({"codex"}),
        available_profiles={"codex-luna-high": profile},
    )
    assert route_decision.chosen_profile == "codex-luna-high"
    assert route_decision.model == "gpt-5.6-luna"
    assert route_decision.reasoning_effort == "high"

    routed_state = state.model_copy(update={"route": route_decision})
    dispatch_output = dispatch_job(routed_state)
    dispatched_state = routed_state.model_copy(update={"dispatch": dispatch_output["dispatch"]})

    request = _build_worker_request(dispatched_state)
    assert request.runtime_manifest is not None
    worker_manifest = request.runtime_manifest.get("worker", {})
    assert worker_manifest.get("model") == "gpt-5.6-luna"
    assert worker_manifest.get("reasoning_effort") == "high"
    assert worker_manifest.get("worker_profile") == "codex-luna-high"
    assert request.model_execution is not None
    assert request.model_execution.provider == "codex"
    assert request.model_execution.model == "gpt-5.6-luna"
    assert request.model_execution.reasoning_effort == "high"
    assert worker_manifest.get("model_execution") is not None


def test_envelope_model_execution_honors_worker_adapter_over_provider_default() -> None:
    from orchestrator.graph import _resolve_request_model_execution_for_envelope
    from workers.base import ModelExecutionMetadata

    class _WorkerWithAdapter:
        def _resolve_request_model_execution(
            self, request: WorkerRequest
        ) -> ModelExecutionMetadata:
            del request
            return ModelExecutionMetadata(
                provider="codex",
                model="gpt-5.6-luna",
                reasoning_effort="high",
                model_source="environment",
            )

    worker = _WorkerWithAdapter()
    default_meta = ModelExecutionMetadata(
        provider="codex",
        model="gpt-6-luna",
        reasoning_effort="high",
        model_source="provider_default",
    )
    request = WorkerRequest(
        task_text="test",
        repo_url="https://example.com/repo.git",
        model_execution=default_meta,
    )
    resolved = _resolve_request_model_execution_for_envelope(worker, request, "codex")
    assert resolved is not None
    assert resolved.model == "gpt-5.6-luna"
    assert resolved.model_source == "environment"


def test_aggregate_decomposed_results_ignores_skipped_outcomes() -> None:
    from workers.base import ModelExecutionMetadata

    meta = ModelExecutionMetadata(
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
        model_source="worker_profile",
    )
    success_result = WorkerResult(
        status="success",
        summary="Node 1 done",
        model_execution=meta,
    )
    skipped_result = WorkerResult(
        status="failure",
        summary="Node 2 skipped",
        execution_not_started=True,
        model_execution=None,
    )
    outcomes = [
        NodeOutcome(node_id="n1", status="completed", result=success_result, dependencies=[]),
        NodeOutcome(node_id="n2", status="skipped", result=skipped_result, dependencies=["n1"]),
    ]
    aggregated = _aggregate_decomposed_results(outcomes)
    assert aggregated.model_execution == meta


def test_aggregate_decomposed_results_detects_mixed_identities() -> None:
    from workers.base import ModelExecutionMetadata

    meta1 = ModelExecutionMetadata(
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
        model_source="worker_profile",
    )
    meta2 = ModelExecutionMetadata(
        provider="codex",
        model="gpt-5.2-preview",
        reasoning_effort="low",
        model_source="worker_profile",
    )
    r1 = WorkerResult(status="success", summary="Node 1 done", model_execution=meta1)
    r2 = WorkerResult(status="success", summary="Node 2 done", model_execution=meta2)
    outcomes = [
        NodeOutcome(node_id="n1", status="completed", result=r1, dependencies=[]),
        NodeOutcome(node_id="n2", status="completed", result=r2, dependencies=[]),
    ]
    aggregated = _aggregate_decomposed_results(outcomes)
    assert aggregated.model_execution is None


def test_envelope_model_execution_returns_none_when_worker_resolves_none() -> None:
    from orchestrator.graph import _resolve_request_model_execution_for_envelope
    from workers.base import ModelExecutionMetadata

    class _NoneResolvingWorker:
        def _resolve_request_model_execution(
            self, request: WorkerRequest
        ) -> ModelExecutionMetadata | None:
            del request
            return None

    worker = _NoneResolvingWorker()
    default_meta = ModelExecutionMetadata(
        provider="antigravity",
        model="gemini-3.8-flash",
        reasoning_effort="medium",
        model_source="provider_default",
    )
    request = WorkerRequest(
        task_text="test",
        repo_url="https://example.com/repo.git",
        model_execution=default_meta,
    )
    resolved = _resolve_request_model_execution_for_envelope(worker, request, "gemini")
    assert resolved is None


def test_envelope_model_execution_does_not_short_circuit_on_partial_override() -> None:
    from orchestrator.graph import _resolve_request_model_execution_for_envelope
    from workers.base import ModelExecutionMetadata

    called = False

    class _WorkerWithAdapter:
        def _resolve_request_model_execution(
            self, request: WorkerRequest
        ) -> ModelExecutionMetadata:
            nonlocal called
            called = True
            return ModelExecutionMetadata(
                provider="codex",
                model=request.model_execution.model,
                reasoning_effort="low",
                model_source="task_override",
                reasoning_effort_source="environment",
            )

    worker = _WorkerWithAdapter()
    partial_meta = ModelExecutionMetadata(
        provider="codex",
        model="gpt-5.4",
        reasoning_effort="high",
        model_source="task_override",
        reasoning_effort_source="provider_default",
    )
    request = WorkerRequest(
        task_text="test",
        repo_url="https://example.com/repo.git",
        model_execution=partial_meta,
    )
    resolved = _resolve_request_model_execution_for_envelope(worker, request, "codex")
    assert called is True
    assert resolved is not None
    assert resolved.model == "gpt-5.4"
    assert resolved.reasoning_effort == "low"
    assert resolved.reasoning_effort_source == "environment"


def test_await_worker_with_timeout_does_not_coerce_none_runtime_mode_to_native_agent() -> None:
    from orchestrator.graph import _await_worker_with_timeout

    class _SimpleWorker(Worker):
        async def run(self, request: WorkerRequest) -> WorkerResult:
            del request
            return WorkerResult(
                status="success",
                summary="Done",
                budget_usage={"iterations_used": 1},
            )

    worker = _SimpleWorker()
    request = WorkerRequest(
        task_text="test",
        repo_url="https://example.com/repo.git",
        runtime_mode=None,
    )
    result, _ = asyncio.run(
        _await_worker_with_timeout(
            worker,
            request,
            worker_type="codex",
            session_id="session-1",
            timeout_seconds=5.0,
        )
    )
    assert result.budget_usage == {"iterations_used": 1}
    assert "native_agent" not in result.budget_usage
    assert result.model_execution is not None

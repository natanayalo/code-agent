"""Integration tests for orchestrator graph routing and profile selection."""

from __future__ import annotations

import asyncio

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from tests.integration.orchestrator_graph_support import StaticWorker
from workers import WorkerProfile


def test_orchestrator_graph_errors_when_selected_worker_is_unavailable() -> None:
    """A manual override for an unconfigured worker must fail explicitly, not silently fall back."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(worker=worker)

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit gemini override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_override": "gemini",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_worker == "gemini"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.route.override_applied is True
    assert state.dispatch.worker_type == "gemini"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert worker.requests == []
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No worker is available for route 'gemini'. Available workers: codex."
    )
    assert state.result.next_action_hint == "configure_requested_worker"
    assert state.progress_updates == [
        "task ingested",
        "task classified as implementation",
        "planning skipped: task is straightforward",
        "task spec and route generated",
        "memory context loaded",
        "approval not required",
        "worker dispatched",
        "worker unavailable: gemini",
        "verification failed",
        "result summarized and session state updated",
        "memory persistence queued",
    ]


def test_orchestrator_graph_errors_when_selected_profile_is_unavailable() -> None:
    """A manual profile override must fail explicitly when profile-aware routing is enabled."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            )
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit openrouter profile override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_profile_override": "openrouter-tool-loop-legacy",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_profile == "openrouter-tool-loop-legacy"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.route.override_applied is True
    assert state.dispatch.worker_type is None
    assert state.dispatch.worker_profile == "openrouter-tool-loop-legacy"
    assert worker.requests == []
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for route 'openrouter-tool-loop-legacy'. "
        "Available profiles: codex-native-executor."
    )
    assert state.result.next_action_hint == "configure_requested_worker_profile"


def test_orchestrator_graph_worker_override_respects_profile_opt_in() -> None:
    """Worker overrides should still fail when profile-aware routing has no matching profile."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/openrouter_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    openrouter_worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/openrouter_worker.py"],
            test_results=[{"name": "unexpected-openrouter-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        openrouter_worker=openrouter_worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            )
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit openrouter worker override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_override": "openrouter",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.route.chosen_worker == "openrouter"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for worker route 'openrouter'. "
        "Available profiles: codex-native-executor."
    )
    assert worker.requests == []
    assert openrouter_worker.requests == []


def test_orchestrator_graph_profile_override_incompatible_with_constraints() -> None:
    """Incompatible profile overrides should report a profile-specific routing error."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["workers/codex_worker.py"],
            test_results=[{"name": "unexpected-worker-call", "status": "passed"}],
            artifacts=[],
            next_action_hint="persist_memory",
            summary=None,
        )
    )
    graph = build_orchestrator_graph(
        worker=worker,
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
                mutation_policy="patch_allowed",
            ),
            "codex-read-only-executor": WorkerProfile(
                name="codex-read-only-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace"],
                mutation_policy="read_only",
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run task with read-only constraint and explicit codex profile",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_profile_override": "codex-native-executor",
                    "constraints": {"read_only": True},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.route_reason == "incompatible_profile"
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No routable worker profile is available for route 'codex-native-executor'. "
        "Available profiles: codex-native-executor, codex-read-only-executor."
    )
    assert state.result.next_action_hint == "configure_requested_worker_profile"
    assert worker.requests == []

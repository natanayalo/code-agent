"""Integration tests for orchestrator graph routing and profile selection."""

from __future__ import annotations

import asyncio

from orchestrator import OrchestratorState, WorkerResult, build_orchestrator_graph
from tests.integration.orchestrator_graph_support import StaticWorker
from workers import WorkerProfile
from workers.facade import WorkerFacade


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
    graph = build_orchestrator_graph(worker=WorkerFacade(codex_worker=worker))

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": "Run a task with explicit antigravity override",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "worker_override": "antigravity",
                }
            }
        )
    )

    state = OrchestratorState.model_validate(raw_output)

    assert state.current_step == "persist_memory"
    assert state.route.chosen_worker == "antigravity"
    assert state.route.route_reason == "runtime_unavailable"
    assert state.route.override_applied is True
    assert state.dispatch.worker_type == "antigravity"
    assert state.dispatch.run_id is None
    assert state.dispatch.workspace_id is None
    assert worker.requests == []
    assert state.result is not None
    assert state.result.status == "failure"
    assert (
        state.result.summary
        == "No worker is available for route 'antigravity'. Available workers: codex."
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
        "worker unavailable: antigravity",
        "verification failed",
        "independent code-change review skipped (no files changed)",
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
        worker=WorkerFacade(codex_worker=worker),
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
        worker=WorkerFacade(codex_worker=worker, openrouter_worker=openrouter_worker),
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
        worker=WorkerFacade(codex_worker=worker),
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


def test_orchestrator_graph_dynamic_routing_by_performance() -> None:
    """Task should dynamically route to the profile with best metrics.

    (antigravity-native-executor for bugfix).
    """
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["src/math_utils.py"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="fixed bug",
        )
    )
    graph = build_orchestrator_graph(
        worker=WorkerFacade(codex_worker=worker, antigravity_worker=worker),
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
            "antigravity-native-executor": WorkerProfile(
                name="antigravity-native-executor",
                worker_type="antigravity",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Fix the zero-division guard in calculate_ratio and add regression tests."
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    # For 'bugfix' task type, antigravity-native-executor has higher success rate (0.95 vs 0.82)
    assert state.route.chosen_profile == "antigravity-native-executor"
    assert state.route.chosen_worker == "antigravity"
    assert state.route.route_reason == "dynamic_performance_routing"
    assert state.route.route_metadata is not None
    assert state.route.route_metadata["task_class"] == "bugfix"
    assert state.route.route_metadata["selected_profile"] == "antigravity-native-executor"


def test_orchestrator_graph_dynamic_routing_bypassed_by_budget_preference() -> None:
    """A task with low-cost preference should bypass dynamic routing.

    It should fall back to the legacy cheap worker (codex).
    """
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["src/math_utils.py"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="fixed bug cheaply",
        )
    )
    graph = build_orchestrator_graph(
        worker=WorkerFacade(codex_worker=worker, antigravity_worker=worker),
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
            "antigravity-native-executor": WorkerProfile(
                name="antigravity-native-executor",
                worker_type="antigravity",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Fix the zero-division guard in calculate_ratio and add regression tests."
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "budget": {"prefer_low_cost": True},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    # Budget preference prefer_low_cost should force selection of codex-native-executor.
    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.chosen_worker == "codex"
    assert state.route.route_reason == "budget_preference"
    assert state.route.route_metadata is None


def test_orchestrator_graph_dynamic_routing_fallback_on_missing_metrics() -> None:
    """If a task class has no metrics, it should fall back to heuristics.

    (cheap worker like codex).
    """
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["src/math_utils.py"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="maintenance done",
        )
    )
    graph = build_orchestrator_graph(
        worker=WorkerFacade(codex_worker=worker, antigravity_worker=worker),
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
            "antigravity-native-executor": WorkerProfile(
                name="antigravity-native-executor",
                worker_type="antigravity",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": ("Upgrade Python version in dependency manifest files."),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    # Classified as 'maintenance' (contains 'upgrade'), which has no metrics
    # in routing_metrics.json.
    # Should fall back to legacy heuristics, choosing codex-native-executor (cheap fallback).
    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.chosen_worker == "codex"
    assert state.route.route_reason == "cheap_mechanical_change"
    assert state.route.route_metadata is None


def test_orchestrator_graph_dynamic_routing_incompatible_profiles_filtered() -> None:
    """Read-only profiles should be chosen if task has read_only constraint.

    This filters out incompatible mutation profiles.
    """
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="read-only scout done",
        )
    )
    graph = build_orchestrator_graph(
        worker=WorkerFacade(codex_worker=worker, antigravity_worker=worker),
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
            "antigravity-native-executor-read-only": WorkerProfile(
                name="antigravity-native-executor-read-only",
                worker_type="antigravity",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
                mutation_policy="read_only",
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Fix the zero-division guard in calculate_ratio and add regression tests."
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                    "constraints": {"read_only": True},
                }
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    # Dynamic routing should pick antigravity-native-executor-read-only since codex-native-executor
    # is not read-only and thus incompatible.
    assert state.route.chosen_profile == "antigravity-native-executor-read-only"
    assert state.route.chosen_worker == "antigravity"
    assert state.route.route_reason == "dynamic_performance_routing"


def test_orchestrator_graph_dynamic_routing_escalation_takes_precedence() -> None:
    """Prior-failure escalation takes precedence over performance-based routing."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            commands_run=[],
            files_changed=["src/math_utils.py"],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="fixed bug after retry",
        )
    )
    graph = build_orchestrator_graph(
        worker=WorkerFacade(codex_worker=worker, antigravity_worker=worker),
        enable_worker_profiles=True,
        worker_profiles={
            "codex-native-executor": WorkerProfile(
                name="codex-native-executor",
                worker_type="codex",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
            "antigravity-native-executor": WorkerProfile(
                name="antigravity-native-executor",
                worker_type="antigravity",
                runtime_mode="native_agent",
                capability_tags=["execution"],
                supported_delivery_modes=["workspace", "branch", "draft_pr"],
            ),
        },
    )

    raw_output = asyncio.run(
        graph.ainvoke(
            {
                "task": {
                    "task_text": (
                        "Fix the zero-division guard in calculate_ratio and add regression tests."
                    ),
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "branch": "master",
                },
                "dispatch": {
                    "worker_type": "antigravity",
                    "worker_profile": "antigravity-native-executor",
                },
                "result": {
                    "status": "failure",
                    "failure_kind": "provider_error",
                    "commands_run": [],
                    "files_changed": [],
                },
                "attempt_count": 1,
            }
        )
    )
    state = OrchestratorState.model_validate(raw_output)

    # Because antigravity has already failed once, prior-failure escalation takes precedence
    # and routes to the alternate worker (codex-native-executor).
    assert state.route.chosen_profile == "codex-native-executor"
    assert state.route.chosen_worker == "codex"
    assert state.route.route_reason == "previous_worker_failed"

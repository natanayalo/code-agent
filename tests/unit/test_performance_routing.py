"""Unit tests for the PerformanceRoutingPolicy dynamic routing logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.performance_routing import PerformanceRoutingPolicy
from workers.base import WorkerProfile


@pytest.fixture
def temp_metrics_path(tmp_path: Path) -> Path:
    metrics = {
        "version": "1.2.3",
        "source": "tests/temp_metrics.json",
        "profiles": {
            "codex-native-executor": {
                "task_classes": {
                    "bugfix": {
                        "success_rate": 0.80,
                        "mean_latency_seconds": 100.0,
                    },
                    "scout": {
                        "success_rate": 0.90,
                        "mean_latency_seconds": 50.0,
                    },
                }
            },
            "antigravity-native-executor": {
                "task_classes": {
                    "bugfix": {
                        "success_rate": 0.90,
                        "mean_latency_seconds": 80.0,
                    },
                    "scout": {
                        "success_rate": 0.90,
                        "mean_latency_seconds": 40.0,
                    },
                }
            },
        },
    }
    path = tmp_path / "routing_metrics.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f)
    return path


def test_routing_policy_loads_metrics_correctly(temp_metrics_path: Path) -> None:
    policy = PerformanceRoutingPolicy(temp_metrics_path)
    assert policy.metrics_data["version"] == "1.2.3"
    assert "codex-native-executor" in policy.metrics_data["profiles"]


def test_routing_policy_handles_missing_file(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.json"
    policy = PerformanceRoutingPolicy(missing_path)
    assert policy.metrics_data == {}


def test_routing_policy_handles_malformed_json(tmp_path: Path) -> None:
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("invalid json", encoding="utf-8")
    policy = PerformanceRoutingPolicy(malformed_path)
    assert policy.metrics_data == {}


def test_routing_policy_chooses_best_by_success_rate(temp_metrics_path: Path) -> None:
    policy = PerformanceRoutingPolicy(temp_metrics_path)

    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    antigravity_profile = WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )

    routable = {
        "codex-native-executor": codex_profile,
        "antigravity-native-executor": antigravity_profile,
    }

    # For bugfix, antigravity has success_rate 0.90 vs codex 0.80.
    decision = policy.choose_profile("bugfix", routable)
    assert decision is not None
    assert decision.chosen_profile == "antigravity-native-executor"
    assert decision.chosen_worker == "antigravity"
    assert decision.route_reason == "dynamic_performance_routing"
    assert decision.route_metadata is not None
    assert decision.route_metadata["selected_profile"] == "antigravity-native-executor"


def test_routing_policy_chooses_best_by_latency_tiebreaker(temp_metrics_path: Path) -> None:
    policy = PerformanceRoutingPolicy(temp_metrics_path)

    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    antigravity_profile = WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )

    routable = {
        "codex-native-executor": codex_profile,
        "antigravity-native-executor": antigravity_profile,
    }

    # For scout, both have success_rate 0.90. Antigravity has latency 40.0 vs codex 50.0.
    decision = policy.choose_profile("scout", routable)
    assert decision is not None
    assert decision.chosen_profile == "antigravity-native-executor"
    assert decision.chosen_worker == "antigravity"


def test_routing_policy_normalizes_task_classes(temp_metrics_path: Path) -> None:
    policy = PerformanceRoutingPolicy(temp_metrics_path)

    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    antigravity_profile = WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )

    routable = {
        "codex-native-executor": codex_profile,
        "antigravity-native-executor": antigravity_profile,
    }

    # 'investigation' should map to 'scout'
    decision = policy.choose_profile("investigation", routable)
    assert decision is not None
    assert decision.chosen_profile == "antigravity-native-executor"

    # 'review_fix' should map to 'bugfix'
    decision = policy.choose_profile("review_fix", routable)
    assert decision is not None
    assert decision.chosen_profile == "antigravity-native-executor"


def test_routing_policy_returns_none_if_no_matching_metrics(temp_metrics_path: Path) -> None:
    policy = PerformanceRoutingPolicy(temp_metrics_path)

    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )

    routable = {"codex-native-executor": codex_profile}

    # 'feature' task class has no metrics configured in temporary metrics path.
    decision = policy.choose_profile("feature", routable)
    assert decision is None


def test_routing_policy_handles_json_not_a_dict(tmp_path: Path) -> None:
    path = tmp_path / "list_metrics.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    policy = PerformanceRoutingPolicy(path)
    assert policy.metrics_data == {}


def test_routing_policy_handles_profiles_not_a_dict(tmp_path: Path) -> None:
    path = tmp_path / "profiles_list.json"
    metrics = {"version": "1.0", "profiles": ["codex-native-executor"]}
    with path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f)
    policy = PerformanceRoutingPolicy(path)
    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    decision = policy.choose_profile("bugfix", {"codex-native-executor": codex_profile})
    assert decision is None


def test_routing_policy_handles_malformed_profile_structures(tmp_path: Path) -> None:
    path = tmp_path / "malformed_profile.json"
    metrics = {
        "version": "1.0",
        "profiles": {
            "codex-native-executor": "not-a-dict",
            "antigravity-native-executor": {"task_classes": "not-a-dict"},
            "other-executor": {"task_classes": {"bugfix": "not-a-dict"}},
        },
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f)
    policy = PerformanceRoutingPolicy(path)
    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    antigravity_profile = WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )
    other_profile = WorkerProfile(
        name="other-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )
    routable = {
        "codex-native-executor": codex_profile,
        "antigravity-native-executor": antigravity_profile,
        "other-executor": other_profile,
    }
    decision = policy.choose_profile("bugfix", routable)
    assert decision is None


def test_routing_policy_handles_non_numeric_metrics(tmp_path: Path) -> None:
    path = tmp_path / "non_numeric.json"
    metrics = {
        "version": "1.0",
        "profiles": {
            "codex-native-executor": {
                "task_classes": {
                    "bugfix": {
                        "success_rate": "high",
                        "mean_latency_seconds": 100.0,
                    }
                }
            },
            "antigravity-native-executor": {
                "task_classes": {
                    "bugfix": {
                        "success_rate": 0.9,
                        "mean_latency_seconds": "slow",
                    }
                }
            },
        },
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f)
    policy = PerformanceRoutingPolicy(path)
    codex_profile = WorkerProfile(
        name="codex-native-executor",
        worker_type="codex",
        runtime_mode="native_agent",
    )
    antigravity_profile = WorkerProfile(
        name="antigravity-native-executor",
        worker_type="antigravity",
        runtime_mode="native_agent",
    )
    routable = {
        "codex-native-executor": codex_profile,
        "antigravity-native-executor": antigravity_profile,
    }
    decision = policy.choose_profile("bugfix", routable)
    assert decision is None

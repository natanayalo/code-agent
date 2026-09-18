"""Unit tests for execution identity resolution and cohort isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from db.enums import TimelineEventType
from db.models import OrchestrationRuntime, Task, TaskStatus, WorkerRun, WorkerRuntimeMode
from evaluation.provider_reliability_extractor import _extract_single_task, _validate_task_candidate
from evaluation.provider_reliability_identity import (
    ExecutionIdentity,
    is_execution_identity_matching,
    resolve_task_execution_identity,
)
from evaluation.provider_reliability_models import (
    DEFAULT_EXPECTED_EXECUTION_IDENTITIES,
    ReliabilityReportPolicy,
)


def _make_run(
    profile: str,
    budget_usage: dict[str, Any] | None = None,
) -> WorkerRun:
    run = MagicMock(spec=WorkerRun)
    run.worker_profile = profile
    run.budget_usage = budget_usage
    run.started_at = datetime.now(UTC)
    run.id = "run-1"
    return run


def _make_task(
    chosen_profile: str = "codex-native-executor-read-only",
    runs: list[WorkerRun] | None = None,
    constraints: dict[str, Any] | None = None,
) -> Task:
    task = MagicMock(spec=Task)
    task.chosen_profile = chosen_profile
    task.worker_runs = runs or []
    task.constraints = constraints or {}
    task.status = TaskStatus.COMPLETED
    task.orchestration_runtime = OrchestrationRuntime.TEMPORAL
    task.runtime_mode = WorkerRuntimeMode.NATIVE_AGENT
    task.task_spec = {"task_type": "docs"}
    now = datetime.now(UTC)
    task.created_at = now
    task.updated_at = now
    event = MagicMock()
    event.event_type = TimelineEventType.TASK_COMPLETED
    event.created_at = now
    task.timeline_events = [event]
    task.human_interactions = []
    task.worker_override = None
    task.route_reason = None
    return task


def test_resolve_identity_verified_single_run() -> None:
    """Proves single run with authoritative model_execution returns verified status."""
    budget = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            }
        }
    }
    run = _make_run("codex-native-executor-read-only", budget)
    task = _make_task(runs=[run])

    identity, status = resolve_task_execution_identity(task, [run])
    assert status == "verified"
    assert identity is not None
    assert identity.provider == "codex"
    assert identity.model == "gpt-5.6-luna"
    assert identity.reasoning_effort == "high"


def test_resolve_identity_missing_model_execution_is_unknown_legacy() -> None:
    """Proves missing budget_usage or native_agent metadata maps to unknown_legacy."""
    # Run with empty budget
    run1 = _make_run("codex-native-executor-read-only", {})
    task1 = _make_task(runs=[run1])
    identity, status = resolve_task_execution_identity(task1, [run1])
    assert status == "unknown_legacy"
    assert identity is None

    # Run with native_agent but no model_execution
    run2 = _make_run("codex-native-executor-read-only", {"native_agent": {}})
    task2 = _make_task(runs=[run2])
    identity, status = resolve_task_execution_identity(task2, [run2])
    assert status == "unknown_legacy"
    assert identity is None

    # No runs at all
    task3 = _make_task(runs=[])
    identity, status = resolve_task_execution_identity(task3, [])
    assert status == "unknown_legacy"
    assert identity is None


def test_resolve_identity_mixed_legacy_and_verified_fails_closed() -> None:
    """Proves task with one verified run and one legacy run fails closed as mixed."""
    budget = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            }
        }
    }
    run1 = _make_run("codex-native-executor-read-only", budget)
    run2 = _make_run("codex-native-executor-read-only", {})
    task = _make_task(runs=[run1, run2])

    identity, status = resolve_task_execution_identity(task, [run1, run2])
    assert status == "mixed_execution_identity"
    assert identity is None


def test_resolve_identity_mixed_models_disagreement_fails_closed() -> None:
    """Proves multiple runs with differing models fail closed as mixed."""
    budget1 = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            }
        }
    }
    budget2 = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.4-mini",
                "reasoning_effort": "high",
            }
        }
    }
    run1 = _make_run("codex-native-executor-read-only", budget1)
    run2 = _make_run("codex-native-executor-read-only", budget2)
    task = _make_task(runs=[run1, run2])

    identity, status = resolve_task_execution_identity(task, [run1, run2])
    assert status == "mixed_execution_identity"
    assert identity is None


def test_resolve_identity_multiple_runs_agree() -> None:
    """Proves multiple runs with matching model_execution resolve to verified."""
    budget = {
        "native_agent": {
            "model_execution": {
                "provider": "antigravity",
                "model": "gemini-3.8-flash",
                "reasoning_effort": "medium",
            }
        }
    }
    run1 = _make_run("antigravity-native-executor-read-only", budget)
    run2 = _make_run("antigravity-native-executor-read-only", budget)
    task = _make_task("antigravity-native-executor-read-only", [run1, run2])

    identity, status = resolve_task_execution_identity(task, [run1, run2])
    assert status == "verified"
    assert identity == ExecutionIdentity(
        provider="antigravity",
        model="gemini-3.8-flash",
        reasoning_effort="medium",
    )


def test_no_heuristic_inference_from_profile_name() -> None:
    """Proves profile name never provides model identity without authoritative payload."""
    run = _make_run("codex-native-executor-read-only", None)
    task = _make_task("codex-native-executor-read-only", [run])

    identity, status = resolve_task_execution_identity(task, [run])
    assert status == "unknown_legacy"
    assert identity is None


def test_is_execution_identity_matching() -> None:
    """Test identity equality and mismatch detection."""
    expected = ExecutionIdentity(provider="codex", model="gpt-5.6-luna", reasoning_effort="high")

    assert is_execution_identity_matching(
        ExecutionIdentity(provider="codex", model="gpt-5.6-luna", reasoning_effort="high"),
        expected,
    )
    # Model mismatch
    assert not is_execution_identity_matching(
        ExecutionIdentity(provider="codex", model="gpt-5.4-mini", reasoning_effort="high"),
        expected,
    )
    # Provider mismatch
    assert not is_execution_identity_matching(
        ExecutionIdentity(provider="antigravity", model="gpt-5.6-luna", reasoning_effort="high"),
        expected,
    )
    # Effort mismatch
    assert not is_execution_identity_matching(
        ExecutionIdentity(provider="codex", model="gpt-5.6-luna", reasoning_effort="medium"),
        expected,
    )
    # None handling
    assert not is_execution_identity_matching(None, expected)
    assert not is_execution_identity_matching(expected, None)


def test_cohort_filtering_prevents_two_models_under_same_profile() -> None:
    """Proves legacy or mismatched models are excluded under current_execution_cohort policy.

    This guarantees that two different models under the same profile cannot both enter
    the candidate pool and satisfy the >= 2 candidates rule.
    """
    now = datetime.now(UTC)
    policy = ReliabilityReportPolicy(
        schema_version=2,
        evidence_scope="current_execution_cohort",
        as_of=now,
        window_start_at=now - datetime.resolution * 100000,
        window_end_at=now + datetime.resolution * 100000,
        expected_execution_identities=dict(DEFAULT_EXPECTED_EXECUTION_IDENTITIES),
    )

    # Task executed with deprecated gpt-5.4-mini
    deprecated_budget = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.4-mini",
                "reasoning_effort": "high",
            }
        }
    }
    dep_run = _make_run("codex-native-executor-read-only", deprecated_budget)
    dep_task = _make_task("codex-native-executor-read-only", [dep_run])

    evidence, reason = _extract_single_task(dep_task, policy)
    assert evidence is None
    assert reason == "execution_identity_mismatch"

    # Task executed with legacy run missing model_execution
    legacy_run = _make_run("codex-native-executor-read-only", {})
    legacy_task = _make_task("codex-native-executor-read-only", [legacy_run])

    evidence, reason = _extract_single_task(legacy_task, policy)
    assert evidence is None
    assert reason == "unknown_execution_identity"

    # Task executed with current gpt-5.6-luna
    current_budget = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            }
        }
    }
    current_run = _make_run("codex-native-executor-read-only", current_budget)
    current_task = _make_task("codex-native-executor-read-only", [current_run])

    evidence, reason = _extract_single_task(current_task, policy)
    assert reason is None
    assert evidence is not None
    assert evidence.profile == "codex-native-executor-read-only"


def test_smoke_task_constraint_exclusion() -> None:
    """Proves tasks with exclude_from_provider_reliability are rejected as evaluation_smoke."""
    now = datetime.now(UTC)
    policy = ReliabilityReportPolicy(
        schema_version=2,
        as_of=now,
        window_start_at=now,
        window_end_at=now,
    )
    smoke_task = _make_task(
        constraints={"exclude_from_provider_reliability": True, "read_only": True}
    )
    reason, _, _, _ = _validate_task_candidate(smoke_task, policy)
    assert reason == "evaluation_smoke"


def test_resolve_identity_decomposed_nodes_agreement() -> None:
    """Proves decomposed run with nodes agreeing on model_execution resolves to verified."""
    budget = {
        "node_count": 2,
        "nodes": {
            "1": {
                "native_agent": {
                    "model_execution": {
                        "provider": "codex",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "high",
                    }
                }
            },
            "2": {
                "native_agent": {
                    "model_execution": {
                        "provider": "codex",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "high",
                    }
                }
            },
        },
    }
    run = _make_run("codex-native-executor-read-only", budget)
    task = _make_task("codex-native-executor-read-only", [run])

    identity, status = resolve_task_execution_identity(task, [run])
    assert status == "verified"
    assert identity == ExecutionIdentity(
        provider="codex",
        model="gpt-5.6-luna",
        reasoning_effort="high",
    )


def test_resolve_identity_decomposed_nodes_disagreement() -> None:
    """Proves decomposed plan run with conflicting node models fails closed as unknown/mixed."""
    budget = {
        "node_count": 2,
        "nodes": {
            "1": {
                "native_agent": {
                    "model_execution": {
                        "provider": "codex",
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "high",
                    }
                }
            },
            "2": {
                "native_agent": {
                    "model_execution": {
                        "provider": "codex",
                        "model": "gpt-5.4-mini",
                        "reasoning_effort": "high",
                    }
                }
            },
        },
    }
    run = _make_run("codex-native-executor-read-only", budget)
    task = _make_task("codex-native-executor-read-only", [run])

    identity, status = resolve_task_execution_identity(task, [run])
    assert status == "mixed_execution_identity"
    assert identity is None


def test_unprofiled_retry_causes_mixed_execution_identity() -> None:
    """Proves task with verified run plus unprofiled retry fails closed as mixed."""
    budget_verified = {
        "native_agent": {
            "model_execution": {
                "provider": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
            }
        }
    }
    run_verified = _make_run("codex-native-executor-read-only", budget_verified)
    run_unprofiled = _make_run(None, None)
    task = _make_task("codex-native-executor-read-only", [run_verified, run_unprofiled])

    identity, status = resolve_task_execution_identity(task, [run_verified, run_unprofiled])
    assert status == "mixed_execution_identity"
    assert identity is None

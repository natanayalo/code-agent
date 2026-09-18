"""Execution identity extraction and cohort validation for provider reliability."""

from __future__ import annotations

from typing import Any

from db.models import Task, WorkerRun
from evaluation.provider_reliability_models import (
    ExecutionIdentity,
    ExecutionIdentityStatus,
)


def _extract_run_model_execution(run: WorkerRun) -> dict[str, Any] | None:
    """Safely extract authoritative model_execution dictionary from worker run."""
    if not run.budget_usage or not isinstance(run.budget_usage, dict):
        return None

    # Case 1: Standard single execution run
    native_meta = run.budget_usage.get("native_agent")
    if isinstance(native_meta, dict):
        model_exec = native_meta.get("model_execution")
        if isinstance(model_exec, dict):
            return model_exec

    # Case 2: Decomposed execution with node dictionary
    nodes = run.budget_usage.get("nodes")
    if isinstance(nodes, dict) and nodes:
        node_execs: list[dict[str, Any]] = []
        for node_payload in nodes.values():
            if not isinstance(node_payload, dict):
                return None
            n_meta = node_payload.get("native_agent")
            if not isinstance(n_meta, dict):
                return None
            n_exec = n_meta.get("model_execution")
            if not isinstance(n_exec, dict):
                return None
            node_execs.append(n_exec)
        if not node_execs:
            return None
        first = node_execs[0]
        for other in node_execs[1:]:
            if (
                other.get("provider") != first.get("provider")
                or other.get("model") != first.get("model")
                or other.get("reasoning_effort") != first.get("reasoning_effort")
            ):
                return None
        return first

    return None


def resolve_task_execution_identity(
    task: Task, runs: list[WorkerRun]
) -> tuple[ExecutionIdentity | None, ExecutionIdentityStatus]:
    """Resolve authoritative execution identity across all relevant worker runs for a task.

    Rules:
    - Only budget_usage["native_agent"]["model_execution"] constitutes verified identity.
    - Zero heuristic inference from profile name, runtime manifest, or environment.
    - If no relevant runs exist or none have model_execution: unknown_legacy.
    - If some runs have model_execution and some lack it: mixed_execution_identity.
    - If multiple verified runs disagree on provider, model, or effort: mixed_execution_identity.
    - If all relevant runs agree on identical metadata: verified.
    """
    relevant_runs = runs
    if task.chosen_profile:
        matched = [r for r in runs if r.worker_profile == task.chosen_profile]
        if matched:
            relevant_runs = matched

    if not relevant_runs:
        return None, "unknown_legacy"

    exec_payloads: list[dict[str, Any]] = []
    missing_count = 0

    for r in relevant_runs:
        payload = _extract_run_model_execution(r)
        if payload is None:
            missing_count += 1
        else:
            exec_payloads.append(payload)

    if missing_count == len(relevant_runs):
        return None, "unknown_legacy"

    if missing_count > 0 and exec_payloads:
        return None, "mixed_execution_identity"

    # All runs have model_execution payload; check agreement
    identities: set[tuple[str, str, str | None]] = set()
    for p in exec_payloads:
        provider = p.get("provider")
        model = p.get("model")
        effort = p.get("reasoning_effort")
        if not provider or not isinstance(provider, str) or not model or not isinstance(model, str):
            return None, "mixed_execution_identity"
        identities.add((provider, model, str(effort) if effort is not None else None))

    if len(identities) != 1:
        return None, "mixed_execution_identity"

    resolved_provider, resolved_model, resolved_effort = next(iter(identities))
    return (
        ExecutionIdentity(
            provider=resolved_provider,
            model=resolved_model,
            reasoning_effort=resolved_effort,
        ),
        "verified",
    )


def is_execution_identity_matching(
    actual: ExecutionIdentity | None, expected: ExecutionIdentity | None
) -> bool:
    """Determine whether an actual verified execution identity matches the expected cohort."""
    if actual is None or expected is None:
        return False
    return (
        actual.provider == expected.provider
        and actual.model == expected.model
        and actual.reasoning_effort == expected.reasoning_effort
    )


def check_cohort_membership(
    evidence_scope: str,
    profile: str,
    identity: ExecutionIdentity | None,
    identity_status: ExecutionIdentityStatus,
    expected_identities: dict[str, ExecutionIdentity],
) -> str | None:
    """Check cohort membership and return exclusion reason if task should be excluded."""
    if evidence_scope != "current_execution_cohort":
        return None
    if identity_status == "unknown_legacy":
        return "unknown_execution_identity"
    if identity_status == "mixed_execution_identity":
        return "mixed_execution_identity"
    expected = expected_identities.get(profile)
    if not is_execution_identity_matching(identity, expected):
        return "execution_identity_mismatch"
    return None


__all__ = [
    "ExecutionIdentity",
    "ExecutionIdentityStatus",
    "check_cohort_membership",
    "is_execution_identity_matching",
    "resolve_task_execution_identity",
]

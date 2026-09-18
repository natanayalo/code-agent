"""Execution identity extraction and cohort validation for provider reliability."""

from __future__ import annotations

from typing import Any, Literal

from db.models import Task, WorkerRun
from evaluation.provider_reliability_models import (
    ExecutionIdentity,
    ExecutionIdentityStatus,
)


def _extract_run_model_execution(
    run: WorkerRun,
) -> tuple[dict[str, Any] | None, Literal["verified", "absent", "mixed"]]:
    """Safely extract authoritative model_execution dictionary from worker run."""
    if not run.budget_usage or not isinstance(run.budget_usage, dict):
        return None, "absent"

    # Case 1: Standard single execution run
    native_meta = run.budget_usage.get("native_agent")
    if isinstance(native_meta, dict):
        model_exec = native_meta.get("model_execution")
        if isinstance(model_exec, dict):
            provider = model_exec.get("provider")
            model = model_exec.get("model")
            if provider and isinstance(provider, str) and model and isinstance(model, str):
                return model_exec, "verified"
            return None, "absent"

    # Case 2: Decomposed execution with node dictionary
    nodes = run.budget_usage.get("nodes")
    if isinstance(nodes, dict) and nodes:
        node_execs: list[dict[str, Any]] = []
        for node_payload in nodes.values():
            if not isinstance(node_payload, dict):
                return None, "absent"
            n_meta = node_payload.get("native_agent")
            if not isinstance(n_meta, dict):
                return None, "absent"
            n_exec = n_meta.get("model_execution")
            if not isinstance(n_exec, dict):
                return None, "absent"
            node_execs.append(n_exec)
        if not node_execs:
            return None, "absent"
        first = node_execs[0]
        for other in node_execs[1:]:
            if (
                other.get("provider") != first.get("provider")
                or other.get("model") != first.get("model")
                or other.get("reasoning_effort") != first.get("reasoning_effort")
            ):
                return None, "mixed"
        provider = first.get("provider")
        model = first.get("model")
        if provider and isinstance(provider, str) and model and isinstance(model, str):
            return first, "verified"
        return None, "absent"

    return None, "absent"


def resolve_task_execution_identity(
    task: Task, runs: list[WorkerRun]
) -> tuple[ExecutionIdentity | None, ExecutionIdentityStatus]:
    """Resolve authoritative execution identity across all worker runs for a task.

    Rules:
    - Only budget_usage["native_agent"]["model_execution"] constitutes verified identity.
    - Zero heuristic inference from profile name, runtime manifest, or environment.
    - If no runs exist or all runs lack model_execution: unknown_legacy.
    - If any run has conflicting decomposed node identities: mixed_execution_identity.
    - If some runs have model_execution and some lack it: mixed_execution_identity.
    - If multiple verified runs disagree on provider, model, or effort: mixed_execution_identity.
    - If all runs agree on identical metadata: verified.
    """
    if not runs:
        return None, "unknown_legacy"

    verified_payloads: list[dict[str, Any]] = []
    has_absent = False

    for r in runs:
        payload, run_status = _extract_run_model_execution(r)
        if run_status == "mixed":
            return None, "mixed_execution_identity"
        if run_status == "absent":
            has_absent = True
        elif run_status == "verified" and payload is not None:
            verified_payloads.append(payload)

    if has_absent and verified_payloads:
        return None, "mixed_execution_identity"

    if has_absent and not verified_payloads:
        return None, "unknown_legacy"

    identities: set[tuple[str, str, str | None]] = set()
    for p in verified_payloads:
        provider = str(p["provider"])
        model = str(p["model"])
        effort = str(p["reasoning_effort"]) if p.get("reasoning_effort") is not None else None
        identities.add((provider, model, effort))

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

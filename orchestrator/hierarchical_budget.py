"""Typed hierarchical budget contracts, enforcement gates, and compliance evaluation."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC
from typing import Any, Literal

from pydantic import Field

from orchestrator.state import OrchestratorModel, OrchestratorState, WorkerResult
from tools.numeric import coerce_non_negative_int_like, coerce_positive_int_like

# Default Whole-Task and Hierarchical Limits
DEFAULT_INTERACTIVE_TASK_WALL_TIME_SECONDS = 1200
DEFAULT_UNATTENDED_TASK_WALL_TIME_SECONDS = 600
DEFAULT_SCOUT_TASK_WALL_TIME_SECONDS = 180
DEFAULT_DEEP_SCOUT_TASK_WALL_TIME_SECONDS = 720
GLOBAL_MAX_TASK_WALL_TIME_SECONDS = 1800
DEFAULT_INTERACTIVE_MAX_WORKER_CALLS = 6
DEFAULT_UNATTENDED_MAX_WORKER_CALLS = 4
DEFAULT_SCOUT_MAX_WORKER_CALLS = 1
DEFAULT_DEEP_SCOUT_MAX_WORKER_CALLS = 2
GLOBAL_MAX_WORKER_CALLS = 16
DEFAULT_MAX_CONCURRENT_NODES = 2
SCOUT_MAX_CONCURRENT_NODES = 1
GLOBAL_MAX_CONCURRENT_NODES = 2
GLOBAL_MAX_NODE_ATTEMPTS = 3
GLOBAL_MAX_REPAIR_PASSES = 4
GLOBAL_MAX_VERIFIER_PASSES = 5
GLOBAL_MAX_REVIEW_REPAIR_PASSES = 3

ExhaustedDimension = Literal[
    "max_worker_calls",
    "max_task_wall_time_seconds",
    "max_repair_passes",
    "max_node_attempts",
]


class SliceBudgetLimits(OrchestratorModel):
    """Budget limits for one execution slice (primary, node, or repair)."""

    timeout_seconds: int = Field(gt=0)
    max_iterations: int = Field(gt=0)
    max_tool_calls: int = Field(ge=0)
    max_shell_commands: int = Field(ge=0)
    max_retries: int = Field(ge=0)


class NodeBudgetLimits(SliceBudgetLimits):
    """Slice budget limits for DAG child node execution."""

    max_node_attempts: int = Field(default=2, ge=1, le=GLOBAL_MAX_NODE_ATTEMPTS)


class RepairBudgetLimits(SliceBudgetLimits):
    """Slice budget limits for bounded verifier or review repair passes."""

    max_repair_passes: int = Field(default=2, ge=0, le=GLOBAL_MAX_REPAIR_PASSES)
    max_verifier_passes: int = Field(default=1, ge=0, le=GLOBAL_MAX_VERIFIER_PASSES)
    max_review_repair_passes: int = Field(default=1, ge=0, le=GLOBAL_MAX_REVIEW_REPAIR_PASSES)


class BudgetSpec(OrchestratorModel):
    """Hierarchical budget specification across whole-task and execution slices."""

    schema_version: Literal[1] = 1
    execution_mode: Literal["interactive", "unattended"] = "unattended"
    max_task_wall_time_seconds: int = Field(gt=0, le=GLOBAL_MAX_TASK_WALL_TIME_SECONDS)
    max_worker_calls: int = Field(gt=0, le=GLOBAL_MAX_WORKER_CALLS)
    max_concurrent_nodes: int = Field(default=2, ge=1, le=GLOBAL_MAX_CONCURRENT_NODES)
    primary: SliceBudgetLimits
    node: NodeBudgetLimits
    repair: RepairBudgetLimits


class HierarchicalBudgetLedger(OrchestratorModel):
    """Runtime ledger recording multi-level budget consumption and headroom."""

    schema_version: Literal[1] = 1
    worker_calls_used: int = Field(default=0, ge=0)
    max_worker_calls: int = Field(gt=0)
    wall_clock_seconds_used: float = Field(default=0.0, ge=0.0)
    max_task_wall_time_seconds: int = Field(gt=0)
    remaining_worker_calls: int = Field(default=0, ge=0)
    remaining_wall_time_seconds: float = Field(default=0.0, ge=0.0)
    repair_passes_used: int = Field(default=0, ge=0)
    max_repair_passes: int = Field(default=0, ge=0)
    verifier_repair_passes_used: int = Field(default=0, ge=0)
    review_repair_passes_used: int = Field(default=0, ge=0)
    max_concurrent_nodes: int = Field(default=2, ge=1, le=GLOBAL_MAX_CONCURRENT_NODES)
    active_slice: Literal["primary", "node", "repair"] = "primary"
    exhausted_dimension: ExhaustedDimension | None = None


class HierarchicalBudgetEvaluationReport(OrchestratorModel):
    """Deterministic M29 evaluation report assessing hierarchical budget compliance."""

    schema_version: Literal[1] = 1
    compliant: bool
    violations: list[str] = Field(default_factory=list)
    task_wall_time_compliant: bool = True
    worker_calls_compliant: bool = True
    node_attempts_compliant: bool = True
    repair_passes_compliant: bool = True
    concurrency_compliant: bool = True
    spec: dict[str, Any]
    observed_usage: dict[str, Any]


def _resolve_mode_string(
    budget: Mapping[str, Any], constraints: Mapping[str, Any] | None, channel: str
) -> Literal["interactive", "unattended"]:
    raw = (
        str((constraints or {}).get("execution_mode") or budget.get("execution_mode") or "")
        .strip()
        .lower()
    )
    if raw in ("interactive", "unattended"):
        return "interactive" if raw == "interactive" else "unattended"
    return "interactive" if channel.strip().lower() == "telegram" else "unattended"


def _extract_int(budget: Mapping[str, Any], key: str, cap: int, default: int) -> int:
    val = coerce_positive_int_like(budget.get(key))
    return min(val if val is not None else default, cap)


def _extract_non_neg(budget: Mapping[str, Any], key: str, cap: int, default: int) -> int:
    val = coerce_non_negative_int_like(budget.get(key))
    return min(val if val is not None else default, cap)


def _resolve_primary_limits(
    b: Mapping[str, Any], mode: str, is_scout: bool, is_deep: bool
) -> SliceBudgetLimits:
    min_secs = (coerce_positive_int_like(b.get("max_minutes")) or 0) * 60
    raw_timeout = coerce_positive_int_like(b.get("worker_timeout_seconds")) or (
        min_secs if min_secs > 0 else None
    )
    if is_deep:
        t, it, tc, r = 360, 5, 16, 0
    elif is_scout:
        t, it, tc, r = 180, 3, 8, 0
    elif mode == "interactive":
        t, it, tc, r = 600, 8, 24, 2
    else:
        t, it, tc, r = 300, 5, 12, 1

    return SliceBudgetLimits(
        timeout_seconds=min(raw_timeout or t, 900),
        max_iterations=_extract_int(b, "max_iterations", 20, it),
        max_tool_calls=_extract_non_neg(b, "max_tool_calls", 100, tc),
        max_shell_commands=_extract_non_neg(b, "max_shell_commands", 100, tc),
        max_retries=_extract_non_neg(b, "max_retries", 10, r),
    )


def _resolve_whole_task_limits(
    b: Mapping[str, Any], primary: SliceBudgetLimits, mode: str, is_scout: bool, is_deep: bool
) -> tuple[int, int, int]:
    if is_deep:
        w, c = DEFAULT_DEEP_SCOUT_TASK_WALL_TIME_SECONDS, DEFAULT_DEEP_SCOUT_MAX_WORKER_CALLS
    elif is_scout:
        w, c = DEFAULT_SCOUT_TASK_WALL_TIME_SECONDS, DEFAULT_SCOUT_MAX_WORKER_CALLS
    elif mode == "interactive":
        w, c = DEFAULT_INTERACTIVE_TASK_WALL_TIME_SECONDS, DEFAULT_INTERACTIVE_MAX_WORKER_CALLS
    else:
        w, c = DEFAULT_UNATTENDED_TASK_WALL_TIME_SECONDS, DEFAULT_UNATTENDED_MAX_WORKER_CALLS

    raw_wall = coerce_positive_int_like(b.get("max_task_wall_time_seconds")) or w
    max_wall = min(max(raw_wall, primary.timeout_seconds), GLOBAL_MAX_TASK_WALL_TIME_SECONDS)
    max_calls = _extract_int(b, "max_worker_calls", GLOBAL_MAX_WORKER_CALLS, c)
    def_conc = SCOUT_MAX_CONCURRENT_NODES if is_scout else DEFAULT_MAX_CONCURRENT_NODES
    max_conc = (
        SCOUT_MAX_CONCURRENT_NODES
        if is_scout
        else _extract_int(b, "max_concurrent_nodes", GLOBAL_MAX_CONCURRENT_NODES, def_conc)
    )
    return max_wall, max_calls, max_conc


def _resolve_slice_base(
    b: Mapping[str, Any], prefix: str, pri: SliceBudgetLimits, max_wall: int
) -> dict[str, int]:
    return {
        "timeout_seconds": min(
            _extract_int(b, f"{prefix}_timeout_seconds", pri.timeout_seconds, pri.timeout_seconds),
            max_wall,
        ),
        "max_iterations": _extract_int(
            b, f"{prefix}_max_iterations", pri.max_iterations, pri.max_iterations
        ),
        "max_tool_calls": _extract_non_neg(
            b, f"{prefix}_max_tool_calls", pri.max_tool_calls, pri.max_tool_calls
        ),
        "max_shell_commands": _extract_non_neg(
            b, f"{prefix}_max_shell_commands", pri.max_shell_commands, pri.max_shell_commands
        ),
        "max_retries": _extract_non_neg(
            b, f"{prefix}_max_retries", pri.max_retries, pri.max_retries
        ),
    }


def _resolve_node_limits(
    b: Mapping[str, Any], pri: SliceBudgetLimits, max_wall: int, is_scout: bool
) -> NodeBudgetLimits:
    base = _resolve_slice_base(b, "node", pri, max_wall)
    node_att = _extract_int(b, "max_node_attempts", GLOBAL_MAX_NODE_ATTEMPTS, 1 if is_scout else 2)
    return NodeBudgetLimits(**base, max_node_attempts=node_att)


def _resolve_repair_limits(
    b: Mapping[str, Any], pri: SliceBudgetLimits, max_wall: int, max_calls: int, is_scout: bool
) -> RepairBudgetLimits:
    base = _resolve_slice_base(b, "repair", pri, max_wall)
    max_repair = (
        0
        if is_scout
        else _extract_non_neg(b, "max_repair_passes", GLOBAL_MAX_REPAIR_PASSES, min(2, max_calls))
    )
    ver_p = (
        0
        if is_scout
        else min(
            _extract_non_neg(b, "max_verifier_passes", GLOBAL_MAX_VERIFIER_PASSES, 1),
            max_repair,
        )
    )
    rev_p = (
        0
        if is_scout
        else min(
            _extract_non_neg(b, "max_review_repair_passes", GLOBAL_MAX_REVIEW_REPAIR_PASSES, 1),
            max_repair,
        )
    )
    return RepairBudgetLimits(
        **base,
        max_repair_passes=max_repair,
        max_verifier_passes=ver_p,
        max_review_repair_passes=rev_p,
    )


def resolve_budget_spec(
    budget: Mapping[str, Any] | None,
    *,
    constraints: Mapping[str, Any] | None = None,
    channel: str = "http",
) -> BudgetSpec:
    """Resolve a typed BudgetSpec from raw budget and constraint inputs."""
    safe_budget: Mapping[str, Any] = budget or {}
    existing_spec = safe_budget.get("budget_spec")
    if isinstance(existing_spec, dict):
        try:
            return BudgetSpec.model_validate(existing_spec)
        except Exception:
            pass

    safe_constraints = constraints or {}
    mode = _resolve_mode_string(safe_budget, safe_constraints, channel)
    is_scout = safe_constraints.get("task_type") == "scout"
    is_deep = str(safe_constraints.get("scout_mode") or "").strip() == "deep"

    primary = _resolve_primary_limits(safe_budget, mode, is_scout, is_deep)
    max_wall, max_calls, max_conc = _resolve_whole_task_limits(
        safe_budget, primary, mode, is_scout, is_deep
    )
    node = _resolve_node_limits(safe_budget, primary, max_wall, is_scout)
    repair = _resolve_repair_limits(safe_budget, primary, max_wall, max_calls, is_scout)

    return BudgetSpec(
        execution_mode=mode,
        max_task_wall_time_seconds=max_wall,
        max_worker_calls=max_calls,
        max_concurrent_nodes=max_conc,
        primary=primary,
        node=node,
        repair=repair,
    )


def _compute_calls_used(state: OrchestratorState, repair_passes_used: int = 0) -> int:
    completed = [o for o in state.node_outcomes if o.status != "skipped"]
    if completed:
        return sum(max(o.attempts or 1, 1) for o in completed)
    if state.scout_phase_results:
        return len(state.scout_phase_results)

    evs = ("worker_completed", "worker_failed", "worker_error")
    fin = sum(1 for e in state.timeline_events if e.event_type in evs)
    if state.result is not None:
        return max(fin, (state.attempt_count or 1) + repair_passes_used, 1)

    prior_attempts = max(0, (state.attempt_count or 0) - 1)
    return max(fin, prior_attempts + repair_passes_used)


def _compute_wall_seconds_used(state: OrchestratorState) -> float:
    created_ats = [
        (e.created_at if e.created_at.tzinfo is not None else e.created_at.replace(tzinfo=UTC))
        for e in state.timeline_events
        if e.created_at is not None
    ]
    timeline_span = (
        (max(created_ats) - min(created_ats)).total_seconds() if len(created_ats) >= 2 else 0.0
    )

    all_results = [
        state.result,
        *(o.result for o in state.node_outcomes),
        *(p.result for p in state.scout_phase_results),
    ]
    summed_wall = sum(
        float(r.budget_usage.get("wall_clock_seconds") or 0.0)
        for r in all_results
        if r and r.budget_usage
    )
    return max(max(timeline_span, 0.0), summed_wall)


def _repair_counts(
    c: Mapping[str, Any], u_key: str, fu_key: str, m_key: str = "", fm_key: str = ""
) -> tuple[int, int | None]:
    val_used = coerce_non_negative_int_like(c.get(u_key))
    if val_used is None:
        val_used = coerce_non_negative_int_like(c.get(fu_key))
    cfg_max = coerce_non_negative_int_like(c.get(m_key)) if m_key else None
    if cfg_max is None and fm_key:
        cfg_max = coerce_non_negative_int_like(c.get(fm_key))
    return val_used or 0, cfg_max


def compute_hierarchical_budget_ledger(
    state: OrchestratorState,
    *,
    active_slice: Literal["primary", "node", "repair"] = "primary",
    current_call_wall_seconds: float = 0.0,
    count_current_call: bool = False,
) -> HierarchicalBudgetLedger:
    """Derive current hierarchical budget consumption, remaining quota, and exhaustion."""
    spec = resolve_budget_spec(state.task.budget, constraints=state.task.constraints)
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    verifier_used, _ = _repair_counts(
        constraints, "independent_verifier_repair_passes_used", "verifier_repair_passes_used"
    )
    review_used, _ = _repair_counts(
        constraints, "independent_review_repair_passes_used", "repair_passes_used"
    )
    repair_passes_used = max(verifier_used + review_used, state.completion_loop.repair_pass or 0)

    calls_used = _compute_calls_used(state, repair_passes_used=repair_passes_used) + (
        1 if count_current_call else 0
    )
    wall_seconds_used = _compute_wall_seconds_used(state) + current_call_wall_seconds

    remaining_calls = max(0, spec.max_worker_calls - calls_used)
    remaining_wall_time = max(0.0, float(spec.max_task_wall_time_seconds) - wall_seconds_used)

    exhausted_dimension: ExhaustedDimension | None = None

    if remaining_calls <= 0:
        exhausted_dimension = "max_worker_calls"
    elif remaining_wall_time <= 0.0:
        exhausted_dimension = "max_task_wall_time_seconds"
    elif active_slice == "repair" and repair_passes_used >= spec.repair.max_repair_passes:
        exhausted_dimension = "max_repair_passes"

    return HierarchicalBudgetLedger(
        worker_calls_used=calls_used,
        max_worker_calls=spec.max_worker_calls,
        wall_clock_seconds_used=round(wall_seconds_used, 2),
        max_task_wall_time_seconds=spec.max_task_wall_time_seconds,
        remaining_worker_calls=remaining_calls,
        remaining_wall_time_seconds=round(remaining_wall_time, 2),
        repair_passes_used=repair_passes_used,
        max_repair_passes=spec.repair.max_repair_passes,
        verifier_repair_passes_used=verifier_used,
        review_repair_passes_used=review_used,
        max_concurrent_nodes=spec.max_concurrent_nodes,
        active_slice=active_slice,
        exhausted_dimension=exhausted_dimension,
    )


def effective_slice_budget(
    state: OrchestratorState,
    *,
    active_slice: Literal["primary", "node", "repair"],
) -> dict[str, Any]:
    """Return task budget clamped to active slice limits and remaining whole-task wall time."""
    spec = resolve_budget_spec(state.task.budget, constraints=state.task.constraints)
    ledger = compute_hierarchical_budget_ledger(state, active_slice=active_slice)
    limits = {"primary": spec.primary, "node": spec.node, "repair": spec.repair}[active_slice]
    clamped_timeout = min(limits.timeout_seconds, max(1, int(ledger.remaining_wall_time_seconds)))

    return {
        **state.task.budget,
        "worker_timeout_seconds": clamped_timeout,
        "max_iterations": limits.max_iterations,
        "max_tool_calls": limits.max_tool_calls,
        "max_shell_commands": limits.max_shell_commands,
        "max_retries": limits.max_retries,
        "budget_spec": spec.model_dump(mode="json"),
        "hierarchical_budget": ledger.model_dump(mode="json"),
    }


def check_pre_dispatch_budget_gate(
    state: OrchestratorState,
    *,
    active_slice: Literal["primary", "node", "repair"],
) -> tuple[bool, str | None, HierarchicalBudgetLedger]:
    """Evaluate whether the whole-task or slice budget permits worker dispatch."""
    ledger = compute_hierarchical_budget_ledger(state, active_slice=active_slice)
    return ledger.exhausted_dimension is None, ledger.exhausted_dimension, ledger


def attach_hierarchical_budget_usage(
    result: WorkerResult,
    state: OrchestratorState,
    *,
    active_slice: Literal["primary", "node", "repair"],
) -> WorkerResult:
    """Enrich WorkerResult budget_usage while preserving existing keys."""
    current_wall = float((result.budget_usage or {}).get("wall_clock_seconds") or 0.0)
    ledger = compute_hierarchical_budget_ledger(
        state,
        active_slice=active_slice,
        count_current_call=True,
        current_call_wall_seconds=current_wall,
    )
    current_usage = {
        **(result.budget_usage or {}),
        "hierarchical_budget": ledger.model_dump(mode="json"),
    }
    return result.model_copy(update={"budget_usage": current_usage})


def resolve_effective_verifier_repair_budget(state: OrchestratorState) -> tuple[int, int]:
    """Resolve verifier repair limits bounded by repair caps and remaining task headroom."""
    spec = resolve_budget_spec(state.task.budget, constraints=state.task.constraints)
    ledger = compute_hierarchical_budget_ledger(state, active_slice="repair")
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}

    used, configured = _repair_counts(
        constraints,
        "independent_verifier_repair_passes_used",
        "verifier_repair_passes_used",
        "independent_verifier_max_repair_passes",
        "verifier_repair_max_passes",
    )
    base_max = (
        min(configured, spec.repair.max_verifier_passes)
        if configured is not None
        else spec.repair.max_verifier_passes
    )

    remaining_repairs = max(0, spec.repair.max_repair_passes - ledger.repair_passes_used)
    effective_max = min(base_max, used + remaining_repairs)

    if ledger.remaining_worker_calls <= 0 or ledger.remaining_wall_time_seconds <= 0:
        effective_max = min(effective_max, used)

    return effective_max, used


def resolve_effective_review_repair_budget(
    constraints: Mapping[str, Any],
    *,
    state: OrchestratorState | None = None,
) -> tuple[int, int]:
    """Resolve review repair limits bounded by repair caps and remaining task headroom."""
    used, configured = _repair_counts(
        constraints,
        "independent_review_repair_passes_used",
        "repair_passes_used",
        "independent_review_max_repair_passes",
        "repair_max_passes",
    )
    base_max = configured if configured is not None else 1

    if state is None:
        return base_max, used

    spec = resolve_budget_spec(state.task.budget, constraints=state.task.constraints)
    ledger = compute_hierarchical_budget_ledger(state, active_slice="repair")

    allowed_review_max = min(base_max, spec.repair.max_review_repair_passes)
    remaining_repairs = max(0, spec.repair.max_repair_passes - ledger.repair_passes_used)
    effective_max = min(allowed_review_max, used + remaining_repairs)

    if ledger.remaining_worker_calls <= 0 or ledger.remaining_wall_time_seconds <= 0:
        effective_max = min(effective_max, used)

    return effective_max, used


def _check_eval_violations(
    spec: BudgetSpec,
    wall_time: float,
    worker_calls: int,
    repair_passes: int,
    verifier_passes: int,
    review_passes: int,
    observed_concurrency: int,
    node_attempts: Mapping[str, int],
) -> tuple[bool, bool, bool, bool, bool, list[str]]:
    violations: list[str] = []
    wall_ok = wall_time <= spec.max_task_wall_time_seconds
    if not wall_ok:
        violations.append(
            f"max_task_wall_time_seconds_exceeded: observed {wall_time}s > limit "
            f"{spec.max_task_wall_time_seconds}s"
        )
    calls_ok = worker_calls <= spec.max_worker_calls
    if not calls_ok:
        violations.append(
            f"max_worker_calls_exceeded: observed {worker_calls} > limit {spec.max_worker_calls}"
        )
    node_ok = True
    for node_id, attempts in node_attempts.items():
        if attempts > spec.node.max_node_attempts:
            node_ok = False
            violations.append(
                f"node_attempts_exceeded:{node_id}: observed {attempts} > limit "
                f"{spec.node.max_node_attempts}"
            )
    repairs_ok = (
        repair_passes <= spec.repair.max_repair_passes
        and verifier_passes <= spec.repair.max_verifier_passes
        and review_passes <= spec.repair.max_review_repair_passes
    )
    if not repairs_ok:
        violations.append(
            f"repair_passes_exceeded: observed {repair_passes} > "
            f"limit {spec.repair.max_repair_passes}"
        )
    conc_ok = observed_concurrency <= spec.max_concurrent_nodes
    if not conc_ok:
        violations.append(
            f"max_concurrent_nodes_exceeded: observed {observed_concurrency} > limit "
            f"{spec.max_concurrent_nodes}"
        )
    return wall_ok, calls_ok, node_ok, repairs_ok, conc_ok, violations


def evaluate_hierarchical_budget_compliance(
    spec: BudgetSpec | Mapping[str, Any],
    usage: Mapping[str, Any],
    *,
    observed_max_concurrency: int = 1,
    observed_node_attempts: Mapping[str, int] | None = None,
) -> HierarchicalBudgetEvaluationReport:
    """Assess execution telemetry compliance against the hierarchical BudgetSpec."""
    budget_spec = spec if isinstance(spec, BudgetSpec) else BudgetSpec.model_validate(spec)
    safe_usage = dict(usage or {})
    h_usage = safe_usage.get("hierarchical_budget") or {}

    wall_time = float(
        h_usage.get("wall_clock_seconds_used") or safe_usage.get("wall_clock_seconds") or 0.0
    )
    worker_calls = int(
        h_usage.get("worker_calls_used")
        or (len(safe_usage.get("nodes", {})) if isinstance(safe_usage.get("nodes"), dict) else 1)
    )
    repair_passes = int(h_usage.get("repair_passes_used") or 0)
    verifier_passes = int(h_usage.get("verifier_repair_passes_used") or 0)
    review_passes = int(h_usage.get("review_repair_passes_used") or 0)

    wall_ok, calls_ok, node_ok, repairs_ok, conc_ok, violations = _check_eval_violations(
        budget_spec,
        wall_time,
        worker_calls,
        repair_passes,
        verifier_passes,
        review_passes,
        observed_max_concurrency,
        observed_node_attempts or {},
    )

    return HierarchicalBudgetEvaluationReport(
        compliant=len(violations) == 0,
        violations=violations,
        task_wall_time_compliant=wall_ok,
        worker_calls_compliant=calls_ok,
        node_attempts_compliant=node_ok,
        repair_passes_compliant=repairs_ok,
        concurrency_compliant=conc_ok,
        spec=budget_spec.model_dump(mode="json"),
        observed_usage=copy.deepcopy(safe_usage),
    )

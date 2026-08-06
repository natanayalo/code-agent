"""Evidence gates, aggregate metrics, and sanitized M25.6 reporting."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from statistics import fmean
from typing import Any, Literal

from evaluation.temporal_reliability_models import (
    BundleIdentity,
    CapturedCaseEvidence,
    OperatorAnnotations,
    ReliabilitySuiteCase,
    SanitizedCaseResult,
    SanitizedReliabilityMetrics,
    SanitizedReliabilityReport,
    TemporalHistoryEvidence,
)

_TERMINAL_EVENTS = {
    "completed": "task_completed",
    "failed": "task_failed",
    "cancelled": "task_cancelled",
}
_TEMPORAL_TERMINAL = {
    "completed": {"completed"},
    "failed": {"completed", "failed"},
    "cancelled": {"cancelled"},
}
_SIGNALS_BY_PROOF = {
    "clarification": "handle_clarification",
    "approval": "handle_approval",
    "permission_escalation": "handle_permission_escalation",
}
_FORBIDDEN_PUBLIC_KEYS = {
    "task_id",
    "task_text",
    "repo_url",
    "repository_url",
    "summary",
    "response",
    "response_data",
    "logs",
    "secrets",
    "notes",
    "uri",
}


def _append_if(failures: list[str], condition: bool, code: str) -> None:
    if condition:
        failures.append(code)


def _timeline_types(database: dict[str, Any]) -> list[str]:
    return [str(event.get("event_type")) for event in database.get("timeline", [])]


def _validation_present(database: dict[str, Any]) -> bool:
    successful = [run for run in database.get("worker_runs", []) if run.get("status") == "success"]
    if not successful:
        return False
    outcome = successful[-1].get("verifier_outcome") or {}
    return outcome.get("status") in {"passed", "warning"} and any(
        outcome.get(key) for key in ("summary", "items", "deterministic_verification")
    )


def _runtime_failures(
    database: dict[str, Any], identity: BundleIdentity, case: ReliabilitySuiteCase
) -> list[str]:
    failures: list[str] = []
    task = database["task"]
    expected_profile = case.expected_profile
    expected_worker = "codex" if expected_profile.startswith("codex-") else "antigravity"
    expected_read_only = case.expected_mode == "read_only"
    _append_if(
        failures, task.get("orchestration_runtime") != "temporal", "runtime.task_not_temporal"
    )
    _append_if(failures, task.get("runtime_mode") != "native_agent", "runtime.task_not_native")
    _append_if(failures, task.get("chosen_profile") != expected_profile, "profile.task_mismatch")
    _append_if(
        failures, task.get("chosen_worker") != expected_worker, "profile.task_worker_mismatch"
    )
    for run in database.get("worker_runs", []):
        _append_if(
            failures,
            run.get("orchestration_runtime") != "temporal",
            "runtime.worker_run_not_temporal",
        )
        _append_if(
            failures,
            run.get("runtime_mode") != "native_agent",
            "runtime.worker_run_not_native",
        )
        _append_if(failures, run.get("worker_profile") != expected_profile, "profile.run_mismatch")
        _append_if(
            failures, run.get("worker_type") != expected_worker, "profile.run_worker_mismatch"
        )
        manifest = run.get("runtime_manifest") or {}
        service = manifest.get("service") or {}
        manifest_worker = manifest.get("worker") or {}
        manifest_task = manifest.get("task") or {}
        _append_if(
            failures, service.get("build_sha") != identity.build_sha, "deployment.build_mismatch"
        )
        _append_if(
            failures,
            service.get("environment") != identity.environment,
            "deployment.environment_mismatch",
        )
        _append_if(
            failures,
            manifest_worker.get("worker_type") != expected_worker,
            "deployment.worker_manifest_mismatch",
        )
        _append_if(
            failures,
            manifest_worker.get("worker_profile") != expected_profile,
            "deployment.profile_manifest_mismatch",
        )
        _append_if(
            failures,
            manifest_worker.get("runtime_mode") != "native_agent",
            "deployment.runtime_manifest_mismatch",
        )
        _append_if(
            failures,
            manifest_task.get("read_only") is not expected_read_only,
            "deployment.mode_manifest_mismatch",
        )
    return failures


def _terminal_failures(
    database: dict[str, Any], temporal: TemporalHistoryEvidence, expected_status: str
) -> list[str]:
    failures: list[str] = []
    actual_status = database["task"].get("status")
    timeline = _timeline_types(database)
    _append_if(failures, actual_status != expected_status, "terminal.postgres_status_mismatch")
    _append_if(
        failures,
        _TERMINAL_EVENTS.get(expected_status) not in timeline,
        "terminal.timeline_missing",
    )
    terminal_events = [event for event in timeline if event in _TERMINAL_EVENTS.values()]
    _append_if(failures, len(terminal_events) != 1, "terminal.timeline_divergence")
    _append_if(
        failures,
        temporal.workflow_status not in _TEMPORAL_TERMINAL.get(expected_status, set()),
        "terminal.temporal_status_mismatch",
    )
    _append_if(failures, temporal.event_count == 0, "temporal.history_empty_or_expired")
    task_id = database["task"].get("id")
    _append_if(
        failures,
        not task_id or temporal.workflow_id != f"task-{task_id}",
        "temporal.workflow_id_mismatch",
    )
    _append_if(failures, not temporal.run_id, "temporal.run_id_missing")
    return failures


def _worker_evidence_failures(database: dict[str, Any], expected_status: str) -> list[str]:
    runs = database.get("worker_runs", [])
    failures: list[str] = []
    _append_if(failures, not runs, "worker_run.missing")
    if expected_status != "cancelled":
        _append_if(
            failures,
            not any(run.get("status") == "success" for run in runs),
            "worker_run.no_success",
        )
    has_artifact = any(run.get("artifact_index") or run.get("artifacts") for run in runs)
    _append_if(failures, not has_artifact, "artifact.missing")
    return failures


def _dag_failures(database: dict[str, Any], *, fanout: bool) -> list[str]:
    nodes = (database.get("execution_plan") or {}).get("nodes") or []
    failures: list[str] = []
    _append_if(failures, len(nodes) < 2, "dag.insufficient_nodes")
    _append_if(
        failures, any(node.get("status") != "completed" for node in nodes), "dag.node_incomplete"
    )
    _append_if(
        failures, not any(node.get("depends_on") for node in nodes), "dag.dependencies_missing"
    )
    _append_if(
        failures, any(not node.get("attempts") for node in nodes), "dag.attempt_evidence_missing"
    )
    if fanout:
        safe_nodes = [
            node
            for node in nodes
            if node.get("parallel_safe") is True
            and node.get("execution_mode") == "read_only"
            and node.get("aggregation_role") != "mutation"
        ]
        _append_if(failures, len(safe_nodes) < 2, "fanout.safe_nodes_missing")
    return failures


def _interaction_failure(database: dict[str, Any], proof: str) -> bool:
    interactions = database.get("interactions", [])
    if proof == "clarification":
        return not any(
            row.get("interaction_type") == "clarification" and row.get("status") == "resolved"
            for row in interactions
        )
    if proof == "permission_escalation":
        return not any(
            row.get("interaction_type") == "permission" and row.get("status") == "resolved"
            for row in interactions
        )
    return "approval_granted" not in _timeline_types(database)


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _proof_failures(
    case: ReliabilitySuiteCase,
    database: dict[str, Any],
    temporal: TemporalHistoryEvidence,
) -> list[str]:
    failures: list[str] = []
    constraints = database["task"].get("control_constraints") or {}
    activity_count = temporal.activity_counts.get("run_worker", 0)
    for proof in case.required_proofs:
        if proof == "validation":
            _append_if(failures, not _validation_present(database), "proof.validation_missing")
        elif proof == "sequential_dag":
            failures.extend(_dag_failures(database, fanout=False))
            if case.category == "sequential_dag":
                _append_if(
                    failures,
                    temporal.activity_counts.get("run_decomposed_node", 0) < 2,
                    "proof.sequential_activity_missing",
                )
                _append_if(
                    failures,
                    temporal.fanout_overlap,
                    "proof.sequential_activity_overlap",
                )
        elif proof == "fanout_overlap":
            failures.extend(_dag_failures(database, fanout=True))
            _append_if(failures, not temporal.fanout_overlap, "proof.fanout_overlap_missing")
        elif proof == "verifier_repair":
            _append_if(failures, activity_count < 2, "proof.verifier_repair_activity_missing")
            _append_if(
                failures,
                _positive_int(constraints.get("independent_verifier_repair_passes_used")) < 1,
                "proof.verifier_repair_state_missing",
            )
        elif proof == "independent_review_repair":
            _append_if(failures, activity_count < 2, "proof.review_repair_activity_missing")
            _append_if(
                failures,
                _positive_int(constraints.get("independent_review_repair_passes_used")) < 1,
                "proof.review_repair_state_missing",
            )
        elif proof in _SIGNALS_BY_PROOF:
            _append_if(
                failures, _interaction_failure(database, proof), f"proof.{proof}_state_missing"
            )
            _append_if(
                failures,
                _SIGNALS_BY_PROOF[proof] not in temporal.signal_names,
                f"proof.{proof}_signal_missing",
            )
        elif proof == "cancellation":
            _append_if(
                failures,
                "task_cancelled" not in _timeline_types(database),
                "proof.cancellation_missing",
            )
        elif proof == "worker_restart":
            execution_retries = {"run_worker", "run_decomposed_node"} & set(
                temporal.retry_activity_types
            )
            _append_if(failures, not execution_retries, "proof.worker_restart_retry_missing")
        elif proof == "draft_pr":
            deliveries = [
                run.get("delivery_metadata") or {} for run in database.get("worker_runs", [])
            ]
            _append_if(
                failures, not any(row.get("pr_url") for row in deliveries), "proof.draft_pr_missing"
            )
            _append_if(
                failures,
                "delivery_completed" not in _timeline_types(database),
                "proof.draft_pr_delivery_missing",
            )
    return failures


def evaluate_case_gates(
    *,
    case: ReliabilitySuiteCase,
    identity: BundleIdentity,
    database: dict[str, Any],
    temporal: TemporalHistoryEvidence,
    annotations: OperatorAnnotations,
) -> list[str]:
    """Apply deployment, reconciliation, evidence, and scenario gates."""
    failures = _runtime_failures(database, identity, case)
    failures.extend(_terminal_failures(database, temporal, case.expected_terminal_status))
    failures.extend(_worker_evidence_failures(database, case.expected_terminal_status))
    failures.extend(_proof_failures(case, database, temporal))
    actual_status = database["task"].get("status")
    if actual_status == "failed":
        failure_kinds = _failure_kinds(database)
        _append_if(
            failures,
            not failure_kinds or failure_kinds == {"unknown"},
            "failure.kind_missing_or_unknown",
        )
        _append_if(failures, not annotations.next_action, "failure.next_action_missing")
    return sorted(set(failures))


def _failure_kinds(database: dict[str, Any]) -> set[str]:
    kinds = {
        str(run.get("failure_kind")).strip().lower()
        for run in database.get("worker_runs", [])
        if run.get("failure_kind")
    }
    nodes = (database.get("execution_plan") or {}).get("nodes") or []
    kinds.update(
        str(node["failure_kind"]).strip().lower() for node in nodes if node.get("failure_kind")
    )
    return kinds


def _question_count(database: dict[str, Any]) -> int:
    questions: list[str] = []
    for interaction in database.get("interactions", []):
        if interaction.get("interaction_type") != "clarification":
            continue
        raw_questions = (interaction.get("data") or {}).get("questions") or []
        if isinstance(raw_questions, str):
            raw_questions = [raw_questions]
        questions.extend(" ".join(str(question).lower().split()) for question in raw_questions)
    return max(0, len(questions) - len(set(questions)))


def _human_interventions(database: dict[str, Any]) -> int:
    resolved = sum(
        interaction.get("status") in {"resolved", "rejected"}
        for interaction in database.get("interactions", [])
    )
    has_approval = "approval_granted" in _timeline_types(database)
    return resolved + int(has_approval)


def _mean_activity_latency(temporal: TemporalHistoryEvidence) -> dict[str, float]:
    values: dict[str, list[float]] = defaultdict(list)
    for activity in temporal.activities:
        if activity.latency_seconds is not None:
            values[activity.activity_type].append(activity.latency_seconds)
    return {key: round(fmean(items), 6) for key, items in sorted(values.items())}


def _elapsed(database: dict[str, Any], event_type: str) -> float | None:
    created_raw = database["task"].get("created_at")
    event_raw = next(
        (
            event.get("created_at")
            for event in database.get("timeline", [])
            if event.get("event_type") == event_type
        ),
        None,
    )
    if not created_raw or not event_raw:
        return None
    return max(
        0.0,
        (datetime.fromisoformat(event_raw) - datetime.fromisoformat(created_raw)).total_seconds(),
    )


def sanitize_case(capture: CapturedCaseEvidence) -> SanitizedCaseResult:
    """Project private evidence through the explicit public field allowlist."""
    database = capture.database
    expected = capture.expected
    terminal_event = _TERMINAL_EVENTS.get(database["task"].get("status", ""), "task_failed")
    provider_kinds = sorted(kind for kind in _failure_kinds(database) if "provider" in kind)
    has_pr = any(
        (run.get("delivery_metadata") or {}).get("pr_url")
        for run in database.get("worker_runs", [])
    )
    return SanitizedCaseResult(
        case_id=capture.case_id,
        category=expected.category,
        expected_profile=expected.expected_profile,
        expected_terminal_status=expected.expected_terminal_status,
        evidence_reused=capture.source_identity is not None,
        evidence_build_sha=(
            capture.source_identity.build_sha
            if capture.source_identity is not None
            else str(
                (database.get("worker_runs") or [{}])[0]
                .get("runtime_manifest", {})
                .get("service", {})
                .get("build_sha", "unknown")
            )
        ),
        observed_terminal_status=str(database["task"].get("status")),
        valid=not capture.gate_failures,
        gate_failures=capture.gate_failures,
        human_interventions=_human_interventions(database),
        repeated_clarification_questions=_question_count(database),
        manual_log_inspection=capture.annotations.manual_log_inspection,
        validation_evidence_present=_validation_present(database),
        provider_failure_kind=provider_kinds[0] if provider_kinds else None,
        activity_stage_latency_seconds=_mean_activity_latency(capture.temporal),
        time_to_terminal_seconds=_elapsed(database, terminal_event),
        time_to_pr_seconds=_elapsed(database, "delivery_completed") if has_pr else None,
        ci_rejection_count=capture.annotations.ci_rejection_count,
        review_rejection_count=capture.annotations.review_rejection_count,
    )


def _optional_mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return round(fmean(present), 6) if present else None


def _aggregate_metrics(cases: list[SanitizedCaseResult]) -> SanitizedReliabilityMetrics:
    profiles: dict[str, list[bool]] = defaultdict(list)
    stages: dict[str, list[float]] = defaultdict(list)
    provider_failures: Counter[str] = Counter()
    for case in cases:
        profiles[case.expected_profile].append(case.observed_terminal_status == "completed")
        provider_failures.update([case.provider_failure_kind] if case.provider_failure_kind else [])
        for stage, latency in case.activity_stage_latency_seconds.items():
            stages[stage].append(latency)
    validation_denominator = [
        case
        for case in cases
        if case.expected_terminal_status == "completed"
        and not case.expected_profile.endswith("-read-only")
    ]
    return SanitizedReliabilityMetrics(
        human_interventions=sum(case.human_interventions for case in cases),
        repeated_clarification_questions=sum(
            case.repeated_clarification_questions for case in cases
        ),
        manual_log_inspection_cases=sum(case.manual_log_inspection for case in cases),
        validation_evidence_rate=(
            round(
                sum(case.validation_evidence_present for case in validation_denominator)
                / len(validation_denominator),
                6,
            )
            if validation_denominator
            else None
        ),
        provider_failures=dict(sorted(provider_failures.items())),
        profile_success_rates={
            profile: round(sum(results) / len(results), 6)
            for profile, results in sorted(profiles.items())
        },
        activity_stage_latency_seconds={
            stage: round(fmean(values), 6) for stage, values in sorted(stages.items())
        },
        mean_time_to_terminal_seconds=_optional_mean(
            case.time_to_terminal_seconds for case in cases
        ),
        mean_time_to_pr_seconds=_optional_mean(case.time_to_pr_seconds for case in cases),
        ci_rejection_count=sum(case.ci_rejection_count for case in cases),
        review_rejection_count=sum(case.review_rejection_count for case in cases),
    )


def build_report(
    identity: BundleIdentity, captures: list[CapturedCaseEvidence]
) -> SanitizedReliabilityReport:
    """Build an incomplete, invalid, or operator-review-ready aggregate."""
    cases = sorted((sanitize_case(capture) for capture in captures), key=lambda case: case.case_id)
    unique_case_count = len({case.case_id for case in cases})
    status: Literal["incomplete", "invalid", "ready_for_operator_review"]
    if len(cases) < 20:
        status = "incomplete"
    elif len(cases) != 20 or unique_case_count != 20 or not all(case.valid for case in cases):
        status = "invalid"
    else:
        status = "ready_for_operator_review"
    return SanitizedReliabilityReport(
        suite_name="m25.6-temporal-reliability-baseline",
        build_sha=identity.build_sha,
        environment=identity.environment,
        status=status,
        captured_cases=len(cases),
        valid_cases=sum(case.valid for case in cases),
        metrics=_aggregate_metrics(cases),
        cases=cases,
    )


def validate_sanitized_payload(value: Any, *, path: str = "report") -> None:
    """Defense in depth: reject forbidden private fields in generated outputs."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"forbidden public report field at {path}.{key}")
            validate_sanitized_payload(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate_sanitized_payload(item, path=f"{path}[{index}]")


def render_markdown(report: SanitizedReliabilityReport) -> str:
    """Render only fields already admitted by the sanitized report contract."""
    payload = report.model_dump(mode="json")
    validate_sanitized_payload(payload)
    metrics = report.metrics
    lines = [
        "# M25.6 Temporal Reliability Baseline",
        "",
        f"- Status: `{report.status}`",
        f"- Build SHA: `{report.build_sha}`",
        f"- Environment: `{report.environment}`",
        (
            f"- Cases: {report.captured_cases}/{report.required_cases} captured; "
            f"{report.valid_cases} valid"
        ),
        f"- Human interventions: {metrics.human_interventions}",
        f"- Repeated clarification questions: {metrics.repeated_clarification_questions}",
        f"- Validation-evidence rate: {metrics.validation_evidence_rate}",
        (f"- CI/review rejections: {metrics.ci_rejection_count}/{metrics.review_rejection_count}"),
        "",
        "| Case | Category | Profile | Evidence | Observed | Valid |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        (
            f"| {case.case_id} | {case.category} | {case.expected_profile} | "
            f"{'reused' if case.evidence_reused else 'current'} ({case.evidence_build_sha[:12]}) | "
            f"{case.observed_terminal_status} | {'yes' if case.valid else 'no'} |"
        )
        for case in report.cases
    )
    return "\n".join(lines) + "\n"

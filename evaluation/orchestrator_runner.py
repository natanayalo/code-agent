"""Orchestrator-path evaluation runner for the frozen suite."""

from __future__ import annotations

import json
import logging

from evaluation.harness import (
    EvaluationRunner,
    normalize_path_for_scoring,
)
from evaluation.models import FrozenTaskCase, ReliabilityMetrics, ReviewOutcome, WorkerOutcome
from orchestrator import OrchestratorState, build_orchestrator_graph
from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.task_spec import is_destructive_task
from workers import ArtifactReference, Worker, WorkerRequest, WorkerResult, WorkerTestResult

logger = logging.getLogger(__name__)


class _FrozenOutcomeWorker(Worker):
    """Worker adapter that serves deterministic per-case outcomes."""

    def __init__(self, outcomes_by_case_id: dict[str, WorkerOutcome]) -> None:
        self._outcomes_by_case_id = dict(outcomes_by_case_id)

    def _generate_mock_review_result(self, outcome: WorkerOutcome) -> WorkerResult:
        """Generate a mock review WorkerResult based on deterministic outcomes."""
        review = outcome.review
        if review is None:
            review_payload = {
                "reviewer_kind": "independent_reviewer",
                "summary": "Mock review: no findings.",
                "confidence": 1.0,
                "outcome": "no_findings",
                "findings": [],
            }
        else:
            findings = []
            # Generate mock actionable findings (high confidence)
            for i in range(review.actionable_findings_count):
                findings.append(
                    {
                        "severity": "high",
                        "category": "logic",
                        "confidence": 0.9,
                        "file_path": f"file_{i}.py",
                        "line_start": 10 + i,
                        "line_end": 10 + i,
                        "title": f"Mock Actionable Finding {i}",
                        "why_it_matters": "critical logic issue",
                    }
                )
            # Generate mock false positive findings (low confidence to trigger suppression)
            for i in range(review.false_positive_findings_count):
                findings.append(
                    {
                        "severity": "low",
                        "category": "style",
                        "confidence": 0.1,
                        "file_path": f"style_{i}.py",
                        "line_start": 100 + i,
                        "line_end": 100 + i,
                        "title": f"Mock False Positive {i}",
                        "why_it_matters": "style issue",
                    }
                )
            review_payload = {
                "reviewer_kind": "independent_reviewer",
                "summary": f"Mock review with {len(findings)} findings.",
                "confidence": 1.0,
                "outcome": "findings" if findings else "no_findings",
                "findings": findings,
            }
        return WorkerResult(
            status="success",
            summary=json.dumps(review_payload),
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[ArtifactReference(name="workspace", uri="file:///tmp/mock-eval-workspace")],
            next_action_hint="summarize_result",
        )

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        case_id_raw = request.constraints.get("evaluation_case_id")
        case_id = case_id_raw if isinstance(case_id_raw, str) else ""
        outcome = self._outcomes_by_case_id.get(case_id)
        if outcome is None:
            return WorkerResult(
                status="failure",
                summary=f"Missing replay outcome for case '{case_id or 'unknown'}'.",
                commands_run=[],
                files_changed=[],
                test_results=[],
                artifacts=[],
            )

        if outcome.tests_passed is None:
            test_results: list[WorkerTestResult] = []
        else:
            test_results = [
                WorkerTestResult(
                    name="frozen-eval",
                    status="passed" if outcome.tests_passed else "failed",
                    details="deterministic replay outcome",
                )
            ]

        if request.task_text == "Perform an independent review of the changes.":
            return self._generate_mock_review_result(outcome)

        return WorkerResult(
            status=outcome.status,
            summary=outcome.summary,
            commands_run=[],
            files_changed=list(outcome.files_changed),
            test_results=test_results,
            artifacts=[ArtifactReference(name="workspace", uri="file:///tmp/mock-eval-workspace")],
            next_action_hint="persist_memory",
        )


# ---------------------------------------------------------------------------
# M20.0 Reliability metric extraction
# ---------------------------------------------------------------------------

# Timeline event type prefixes that signal a human interaction was needed.
_INTERACTION_EVENT_PREFIXES = (
    "human_interaction",
    "clarification",
    "permission",
    "await_clarification",
    "await_permission",
)


def _extract_worker_metrics(
    result: WorkerResult | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    int,
    int,
    int,
    int,
]:
    if result is None:
        return None, None, None, 0, 0, 0, 0

    return (
        result.status,
        result.failure_kind if result.status != "success" else None,
        result.next_action_hint,
        len(result.friction_reports or []),
        len(result.files_changed or []),
        len(result.commands_run or []),
        len(result.test_results or []),
    )


def _compute_manual_log_inspection(result: WorkerResult | None) -> bool:
    if result is None:
        logger.warning("Worker result is None; falling back to requiring manual inspection.")
        return True
    if result.status == "success":
        return False
    return (
        result.failure_kind in (None, "unknown")
        or not result.next_action_hint
        or not result.friction_reports
    )


def _extract_interaction_metrics(state: OrchestratorState) -> tuple[int, int]:
    interaction_event_types = [
        event.event_type
        for event in state.timeline_events
        if event.event_type
        and any(event.event_type.startswith(prefix) for prefix in _INTERACTION_EVENT_PREFIXES)
    ]
    human_interaction_count = len(interaction_event_types)
    interaction_type_counts: dict[str, int] = {}
    for event_type in interaction_event_types:
        interaction_type_counts[event_type] = interaction_type_counts.get(event_type, 0) + 1
    repeated_question_count = sum(
        count - 1 for count in interaction_type_counts.values() if count > 1
    )
    return human_interaction_count, repeated_question_count


def _extract_stage_latency(state: OrchestratorState) -> tuple[tuple[tuple[str, float], ...], bool]:
    stage_latency: dict[str, float] = {}
    timestamped_events = sorted(
        [event for event in state.timeline_events if event.created_at is not None],
        key=lambda event: event.created_at,
    )
    for i in range(1, len(timestamped_events)):
        prev = timestamped_events[i - 1]
        curr = timestamped_events[i]
        elapsed = max(0.0, (curr.created_at - prev.created_at).total_seconds())
        stage = curr.event_type or "unknown"
        stage_latency[stage] = stage_latency.get(stage, 0.0) + elapsed
    return tuple(sorted(stage_latency.items())), bool(stage_latency)


def _extract_reliability_metrics(state: OrchestratorState) -> ReliabilityMetrics:
    """Derive M20.0 reliability signals from a completed OrchestratorState.

    All derivations use fields that actually exist on OrchestratorState.
    Fields that are only accurate in live runs are documented with their
    replay-mode limitations.
    """
    result = state.result
    approval = state.approval

    (
        worker_status,
        worker_failure_kind,
        next_action_hint,
        friction_report_count,
        files_changed_count,
        commands_run_count,
        test_results_count,
    ) = _extract_worker_metrics(result)
    validation_evidence_present = state.verification is not None or test_results_count > 0
    manual_log_inspection_needed = _compute_manual_log_inspection(result)
    approval_required = approval.required if approval is not None else False
    approval_status: str | None = approval.status if approval is not None else None
    human_interaction_count, repeated_question_count = _extract_interaction_metrics(state)
    stage_latency_seconds, stage_latency_available = _extract_stage_latency(state)

    return ReliabilityMetrics(
        human_interaction_count=human_interaction_count,
        repeated_question_count=repeated_question_count,
        validation_evidence_present=validation_evidence_present,
        manual_log_inspection_needed=manual_log_inspection_needed,
        worker_status=worker_status,
        worker_failure_kind=worker_failure_kind,
        next_action_hint=next_action_hint,
        friction_report_count=friction_report_count,
        files_changed_count=files_changed_count,
        commands_run_count=commands_run_count,
        test_results_count=test_results_count,
        approval_required=approval_required,
        approval_status=approval_status,
        stage_latency_seconds=stage_latency_seconds,
        stage_latency_available=stage_latency_available,
        attempt_count=state.attempt_count,
    )


class OrchestratorReplayRunner(EvaluationRunner):
    """Execute frozen-suite cases through the real orchestrator graph path."""

    def __init__(
        self,
        outcomes_by_case_id: dict[str, WorkerOutcome],
        *,
        worker_override: str = "codex",
    ) -> None:
        self._worker = _FrozenOutcomeWorker(outcomes_by_case_id=outcomes_by_case_id)
        self._worker_override = worker_override
        self._graph = build_orchestrator_graph(
            worker=self._worker,
            gemini_worker=self._worker,
            checkpointer=create_in_memory_checkpointer(),
        )

    def _extract_review_outcome(self, state: OrchestratorState) -> ReviewOutcome | None:
        """Extract a structured review outcome from the orchestrator state."""
        if state.review is None:
            return None
        # Suppressed findings are a pragmatic proxy for filtered/rejected findings in this path;
        # this is not a strict semantic "incorrect finding" measurement.
        actionable_fingerprint_set = {
            (
                normalize_path_for_scoring(finding.file_path),
                finding.title,
                finding.line_start,
                finding.line_end,
            )
            for finding in state.review.findings
        }
        suppressed_fingerprint_set = {
            (
                normalize_path_for_scoring(suppressed.finding.file_path),
                suppressed.finding.title,
                suppressed.finding.line_start,
                suppressed.finding.line_end,
            )
            for suppressed in state.review.suppressed_findings
            if suppressed.finding is not None
        }
        overlapping_fingerprints = actionable_fingerprint_set & suppressed_fingerprint_set
        if overlapping_fingerprints:
            logger.warning(
                "Independent review output contained overlapping actionable and suppressed "
                "findings; deduplicating by fingerprint with suppression precedence."
            )
        # "Actionable" in this runner currently means "not suppressed by policy filtering".
        actionable_findings_count = len(actionable_fingerprint_set - suppressed_fingerprint_set)
        false_positive_findings_count = len(suppressed_fingerprint_set)
        # Total findings are deduplicated across actionable/suppressed sets.
        total_findings_count = actionable_findings_count + false_positive_findings_count
        repair_attempted = state.repair_handoff_requested
        return ReviewOutcome(
            findings_count=total_findings_count,
            actionable_findings_count=actionable_findings_count,
            false_positive_findings_count=false_positive_findings_count,
            fix_after_review_attempted=bool(repair_attempted),
            fix_after_review_succeeded=(
                state.verification is not None and state.verification.status == "passed"
            )
            if repair_attempted
            else None,
        )

    async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        constraints: dict[str, object] = {
            "evaluation_case_id": case.case_id,
            "execution_mode": "unattended",
        }
        # Frozen eval runs unattended. Pre-seed trusted approval for non-destructive
        # cases so high-risk task-spec gating does not block deterministic scoring.
        if not is_destructive_task(case.task_text, constraints):
            constraints["approval"] = {
                "status": "approved",
                "source": "orchestrator",
                "reason": "Frozen evaluation unattended non-destructive task auto-approved.",
            }

        raw_state = await self._graph.ainvoke(
            {
                "task": {
                    "task_text": case.task_text,
                    "repo_url": f"https://example.invalid/{case.repo_fixture}",
                    "branch": "master",
                    "worker_override": self._worker_override,
                    "constraints": constraints,
                    "budget": {
                        "max_iterations": 1,
                        "max_tool_calls": 0,
                        "max_shell_commands": 0,
                        "worker_timeout_seconds": 30,
                        "orchestrator_timeout_seconds": 35,
                    },
                }
            },
            config={"configurable": {"thread_id": f"frozen-eval-{case.case_id}"}},
        )
        if "__interrupt__" in raw_state:
            return WorkerOutcome(
                status="failure",
                summary=(
                    "Orchestrator execution was interrupted awaiting approval in "
                    "unattended evaluation mode."
                ),
                files_changed=(),
                tests_passed=False,
            )
        state = OrchestratorState.model_validate(raw_state)
        result = state.result
        if result is None:
            return WorkerOutcome(
                status="failure",
                summary="Orchestrator returned without a worker result.",
                files_changed=(),
                tests_passed=False,
            )

        if not result.test_results:
            tests_passed: bool | None = None
        else:
            tests_passed = all(
                test_result.status == "passed" for test_result in result.test_results
            )
        review_outcome = self._extract_review_outcome(state)
        reliability = _extract_reliability_metrics(state)

        return WorkerOutcome(
            status=result.status,
            summary=result.summary or "",
            files_changed=tuple(result.files_changed),
            tests_passed=tests_passed,
            review=review_outcome,
            reliability=reliability,
        )

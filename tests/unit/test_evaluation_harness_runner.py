"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest

from evaluation import (
    OrchestratorReplayRunner,
    TaskExpectation,
    WorkerOutcome,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
)
from evaluation.harness import (
    FrozenTaskCase,
)


def _make_fake_graph(
    expected_thread_id: str,
    repair_handoff: bool,
    review_outcome: str,
    findings: list[dict[str, object]],
    suppressed: list[dict[str, object]],
    review_summary: str = "reviewed",
) -> object:
    class _FakeGraph:
        async def ainvoke(self, _inputs: object, config: dict[str, object]) -> dict[str, object]:
            assert config["configurable"] == {"thread_id": expected_thread_id}
            return {
                "task": {"task_text": "Do a thing"},
                "dispatch": {"worker_type": "codex"},
                "verification": {"status": "passed", "items": []},
                "repair_handoff_requested": repair_handoff,
                "result": {
                    "status": "success",
                    "summary": "completed",
                    "commands_run": [],
                    "files_changed": ["src/app.py"],
                    "test_results": [{"name": "suite", "status": "passed", "details": "ok"}],
                    "artifacts": [],
                },
                "review": {
                    "reviewer_kind": "independent_reviewer",
                    "summary": review_summary,
                    "confidence": 0.8,
                    "outcome": review_outcome,
                    "findings": findings,
                    "suppressed_findings": suppressed,
                },
            }

    return _FakeGraph()


def test_orchestrator_runner_executes_case_through_graph_path() -> None:
    suite = load_frozen_suite()
    case = suite.cases[0]
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id=default_replay_outcomes(suite.cases),
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "zero division" in outcome.summary
    assert set(case.expectation.required_files_changed).issubset(set(outcome.files_changed))
    assert outcome.tests_passed is True


def test_orchestrator_runner_auto_approves_non_destructive_unattended_cases() -> None:
    suite = load_frozen_suite()
    case = next(case for case in suite.cases if case.case_id == "frozen-002")
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id=default_replay_outcomes(suite.cases),
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "authentication" in outcome.summary
    assert set(case.expectation.required_files_changed).issubset(set(outcome.files_changed))
    assert outcome.tests_passed is True


def test_orchestrator_runner_propagates_review_outcome_fields() -> None:
    case = FrozenTaskCase(
        case_id="reviewed-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"reviewed-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-reviewed-case",
        repair_handoff=True,
        review_outcome="findings",
        review_summary="one issue surfaced",
        findings=[
            {
                "severity": "high",
                "category": "logic",
                "confidence": 0.9,
                "file_path": "src/app.py",
                "line_start": 12,
                "line_end": 13,
                "title": "Missing guard",
                "why_it_matters": "Can crash on empty input.",
            }
        ],
        suppressed=[
            {
                "finding": {
                    "severity": "low",
                    "category": "style",
                    "confidence": 0.6,
                    "file_path": "src/app.py",
                    "line_start": 2,
                    "title": "Minor formatting",
                    "why_it_matters": "Consistency",
                },
                "reasons": ["style category suppressed by policy (style)"],
            }
        ],
    )  # type: ignore[assignment]

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.review is not None
    assert outcome.review.findings_count == 2
    assert outcome.review.actionable_findings_count == 1
    assert outcome.review.false_positive_findings_count == 1
    assert outcome.review.fix_after_review_attempted is True
    assert outcome.review.fix_after_review_succeeded is True


def test_orchestrator_runner_review_metrics_precision_reflects_suppressed_findings() -> None:
    case = FrozenTaskCase(
        case_id="review-metrics-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"review-metrics-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-review-metrics-case",
        repair_handoff=False,
        review_outcome="findings",
        review_summary="reviewed",
        findings=[
            {
                "severity": "high",
                "category": "logic",
                "confidence": 0.9,
                "file_path": "src/app.py",
                "line_start": 12,
                "line_end": 13,
                "title": "Missing guard",
                "why_it_matters": "Can crash on empty input.",
            }
        ],
        suppressed=[
            {
                "finding": {
                    "severity": "low",
                    "category": "style",
                    "confidence": 0.6,
                    "file_path": "src/app.py",
                    "line_start": 2,
                    "title": "Minor formatting",
                    "why_it_matters": "Consistency",
                },
                "reasons": ["style category suppressed by policy (style)"],
            }
        ],
    )  # type: ignore[assignment]
    orchestrator_outcome = asyncio.run(runner.run_case(case))
    assert orchestrator_outcome.review is not None

    class _SingleOutcomeRunner:
        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            return orchestrator_outcome

    report = asyncio.run(
        evaluate_suite(
            suite_name="orchestrator-review-metrics",
            cases=(case,),
            runner=_SingleOutcomeRunner(),
        )
    )

    assert report.review_metrics is not None
    assert report.review_metrics.precision == pytest.approx(0.5)
    assert report.review_metrics.false_discovery_rate == pytest.approx(0.5)
    assert report.review_metrics.false_positive_rate == pytest.approx(0.5)


def test_orchestrator_runner_deduplicates_overlapping_suppressed_findings() -> None:
    case = FrozenTaskCase(
        case_id="overlap-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"overlap-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-overlap-case",
        repair_handoff=False,
        review_outcome="findings",
        review_summary="reviewed",
        findings=[
            {
                "severity": "high",
                "category": "logic",
                "confidence": 0.9,
                "file_path": "src/app.py",
                "line_start": 12,
                "line_end": 13,
                "title": "Missing guard",
                "why_it_matters": "Can crash on empty input.",
            }
        ],
        suppressed=[
            {
                "finding": {
                    "severity": "low",
                    "category": "style",
                    "confidence": 0.6,
                    "file_path": "./src/app.py",
                    "line_start": 12,
                    "line_end": 13,
                    "title": "Missing guard",
                    "why_it_matters": "Can crash on empty input.",
                },
                "reasons": ["style category suppressed by policy (style)"],
            }
        ],
    )  # type: ignore[assignment]
    outcome = asyncio.run(runner.run_case(case))

    assert outcome.review is not None
    assert outcome.review.actionable_findings_count == 0
    assert outcome.review.false_positive_findings_count == 1
    assert outcome.review.findings_count == 1
    assert outcome.review.fix_after_review_attempted is False


def test_orchestrator_runner_reports_failure_for_missing_case_outcome() -> None:
    case = FrozenTaskCase(
        case_id="missing-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(outcomes_by_case_id={}, worker_override="codex")

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "failure"
    assert "missing replay outcome" in outcome.summary.lower()


def test_orchestrator_runner_supports_antigravity_override() -> None:
    case = FrozenTaskCase(
        case_id="antigravity-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={
            "antigravity-case": WorkerOutcome(status="success", summary="antigravity path ok")
        },
        worker_override="antigravity",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "antigravity path ok" in outcome.summary


def test_orchestrator_runner_preserves_error_status() -> None:
    case = FrozenTaskCase(
        case_id="error-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"error-case": WorkerOutcome(status="error", summary="worker crashed")},
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "error"
    assert "worker crashed" in outcome.summary


def test_orchestrator_runner_handles_approval_interrupt_as_failure() -> None:
    case = FrozenTaskCase(
        case_id="destructive-case",
        repo_fixture="fixtures/empty",
        task_text="Please rm -rf temporary files",
        expectation=TaskExpectation(require_success=False),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={
            "destructive-case": WorkerOutcome(status="success", summary="would not run")
        },
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "failure"
    assert "interrupted awaiting approval" in outcome.summary.lower()


# ---------------------------------------------------------------------------
# M20.0 ReliabilityMetrics extraction tests
# ---------------------------------------------------------------------------


from evaluation.orchestrator_runner import _extract_reliability_metrics  # noqa: E402


def _make_minimal_state(
    *,
    result_status: str = "success",
    failure_kind: str | None = None,
    next_action_hint: str | None = None,
    friction_reports: list[dict] | None = None,
    verification_present: bool = True,
    approval_required: bool = False,
    approval_status: str = "not_required",
    timeline_events: list[dict] | None = None,
    attempt_count: int = 0,
) -> dict[str, object]:
    """Build a minimal raw state dict acceptable to OrchestratorState.model_validate."""
    result: dict[str, object] = {
        "status": result_status,
        "summary": "done",
        "commands_run": [{"command": "pytest", "exit_code": 0, "duration_seconds": 1.0}],
        "files_changed": ["src/app.py"],
        "test_results": [{"name": "suite", "status": "passed", "details": "ok"}],
        "artifacts": [],
    }
    if failure_kind is not None:
        result["failure_kind"] = failure_kind
    if next_action_hint is not None:
        result["next_action_hint"] = next_action_hint
    if friction_reports is not None:
        result["friction_reports"] = friction_reports
    return {
        "task": {"task_text": "Do a thing"},
        "dispatch": {"worker_type": "codex"},
        "approval": {
            "required": approval_required,
            "status": approval_status,
        },
        "result": result,
        "verification": {"status": "passed", "items": []} if verification_present else None,
        "timeline_events": timeline_events or [],
        "attempt_count": attempt_count,
    }


def _parse_state(raw: dict[str, object]):  # type: ignore[return]
    from orchestrator import OrchestratorState

    return OrchestratorState.model_validate(raw)


def test_extract_reliability_metrics_success_case() -> None:
    state = _parse_state(_make_minimal_state())

    metrics = _extract_reliability_metrics(state)

    assert metrics.worker_status == "success"
    assert metrics.worker_failure_kind is None
    assert metrics.validation_evidence_present is True
    assert metrics.manual_log_inspection_needed is False
    assert metrics.commands_run_count == 1
    assert metrics.files_changed_count == 1
    assert metrics.test_results_count == 1
    assert metrics.attempt_count == 0


def test_extract_reliability_metrics_approval_uses_required_field() -> None:
    state = _parse_state(_make_minimal_state(approval_required=True, approval_status="pending"))

    metrics = _extract_reliability_metrics(state)

    assert metrics.approval_required is True
    assert metrics.approval_status == "pending"


def test_extract_reliability_metrics_approval_required_false_when_not_required() -> None:
    state = _parse_state(
        _make_minimal_state(approval_required=False, approval_status="not_required")
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.approval_required is False
    assert metrics.approval_status == "not_required"


def test_extract_reliability_metrics_defaults_when_approval_missing() -> None:
    state = _parse_state(_make_minimal_state()).model_copy(update={"approval": None})

    metrics = _extract_reliability_metrics(state)

    assert metrics.approval_required is False
    assert metrics.approval_status is None


def test_extract_reliability_metrics_manual_log_inspection_when_unknown_failure() -> None:
    state = _parse_state(
        _make_minimal_state(
            result_status="failure",
            failure_kind="unknown",
            next_action_hint=None,
            friction_reports=[],
        )
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.worker_status == "failure"
    assert metrics.worker_failure_kind == "unknown"
    assert metrics.manual_log_inspection_needed is True


def test_extract_reliability_metrics_no_manual_log_inspection_when_structured() -> None:
    state = _parse_state(
        _make_minimal_state(
            result_status="failure",
            failure_kind="test",
            next_action_hint="retry_with_fix",
            friction_reports=[{"kind": "test_failure"}],
        )
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.manual_log_inspection_needed is False


def test_extract_reliability_metrics_manual_log_inspection_when_no_hint_no_friction() -> None:
    state = _parse_state(
        _make_minimal_state(
            result_status="failure",
            failure_kind="test",
            next_action_hint=None,
            friction_reports=[],
        )
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.manual_log_inspection_needed is True


def test_extract_reliability_metrics_manual_log_inspection_when_result_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _parse_state(_make_minimal_state()).model_copy(update={"result": None})
    caplog.set_level(logging.WARNING, logger="evaluation.orchestrator_runner")

    metrics = _extract_reliability_metrics(state)

    assert metrics.worker_status is None
    assert metrics.manual_log_inspection_needed is True
    assert "Worker result is None" in caplog.text


def test_extract_reliability_metrics_validation_evidence_from_test_results() -> None:
    state = _parse_state(_make_minimal_state(verification_present=False))

    metrics = _extract_reliability_metrics(state)

    # test_results are present (1 item) so validation_evidence_present is True
    assert metrics.validation_evidence_present is True


def test_extract_reliability_metrics_no_validation_evidence_when_empty() -> None:
    raw = _make_minimal_state(verification_present=False)
    # Remove test_results and commands from result
    result: dict[str, object] = dict(raw["result"])  # type: ignore[arg-type]
    result["test_results"] = []
    raw["result"] = result
    state = _parse_state(raw)

    metrics = _extract_reliability_metrics(state)

    assert metrics.validation_evidence_present is False


def test_extract_reliability_metrics_tolerates_nullable_worker_lists() -> None:
    from workers import WorkerResult

    result = WorkerResult.model_construct(
        status="success",
        summary="done",
        failure_kind=None,
        next_action_hint="persist_memory",
        friction_reports=None,
        files_changed=None,
        commands_run=None,
        test_results=None,
    )
    state = _parse_state(_make_minimal_state()).model_copy(
        update={"result": result, "verification": None}
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.friction_report_count == 0
    assert metrics.files_changed_count == 0
    assert metrics.commands_run_count == 0
    assert metrics.test_results_count == 0
    assert metrics.validation_evidence_present is False


def test_extract_reliability_metrics_tolerates_nullable_timeline_events() -> None:
    state = _parse_state(_make_minimal_state()).model_copy(update={"timeline_events": None})

    metrics = _extract_reliability_metrics(state)

    assert metrics.human_interaction_count == 0
    assert metrics.repeated_question_count == 0
    assert metrics.stage_latency_available is False
    assert metrics.stage_latency_seconds == ()


def test_extract_reliability_metrics_stage_latency_not_available_without_timestamps() -> None:
    state = _parse_state(
        _make_minimal_state(
            timeline_events=[
                {"event_type": "dispatch_job", "sequence_number": 0},
                {"event_type": "await_result", "sequence_number": 1},
            ]
        )
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.stage_latency_available is False
    assert metrics.stage_latency_seconds == ()


def test_extract_reliability_metrics_handles_missing_timeline_event_type() -> None:
    from orchestrator.state import TaskTimelineEventState

    timeline_events = [
        TaskTimelineEventState.model_construct(
            event_type="dispatch_job",
            sequence_number=0,
            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
        TaskTimelineEventState.model_construct(
            event_type=None,
            sequence_number=1,
            created_at=datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC),
        ),
        TaskTimelineEventState.model_construct(
            event_type="clarification_requested",
            sequence_number=2,
            created_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        ),
    ]
    state = _parse_state(_make_minimal_state()).model_copy(
        update={"timeline_events": timeline_events}
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.human_interaction_count == 1
    assert metrics.stage_latency_available is True
    assert dict(metrics.stage_latency_seconds) == {
        "clarification_requested": pytest.approx(3.0),
        "unknown": pytest.approx(2.0),
    }


def test_extract_reliability_metrics_sorts_stage_latency_events_by_timestamp() -> None:
    from orchestrator.state import TaskTimelineEventState

    timeline_events = [
        TaskTimelineEventState.model_construct(
            event_type="clarification_requested",
            sequence_number=2,
            created_at=datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),
        ),
        TaskTimelineEventState.model_construct(
            event_type="dispatch_job",
            sequence_number=0,
            created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
        TaskTimelineEventState.model_construct(
            event_type=None,
            sequence_number=1,
            created_at=datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC),
        ),
    ]
    state = _parse_state(_make_minimal_state()).model_copy(
        update={"timeline_events": timeline_events}
    )

    metrics = _extract_reliability_metrics(state)

    assert dict(metrics.stage_latency_seconds) == {
        "clarification_requested": pytest.approx(3.0),
        "unknown": pytest.approx(2.0),
    }


def test_extract_reliability_metrics_human_interaction_estimated_from_timeline() -> None:
    state = _parse_state(
        _make_minimal_state(
            timeline_events=[
                {"event_type": "dispatch_job", "sequence_number": 0},
                {"event_type": "clarification_requested", "sequence_number": 1},
                {"event_type": "permission_denied", "sequence_number": 2},
            ]
        )
    )

    metrics = _extract_reliability_metrics(state)

    assert metrics.human_interaction_count == 2


def test_extract_reliability_metrics_no_interaction_events_gives_zero() -> None:
    state = _parse_state(_make_minimal_state(timeline_events=[]))

    metrics = _extract_reliability_metrics(state)

    assert metrics.human_interaction_count == 0
    assert metrics.repeated_question_count == 0


def test_extract_reliability_metrics_attempt_count_propagated() -> None:
    state = _parse_state(_make_minimal_state(attempt_count=3))

    metrics = _extract_reliability_metrics(state)

    assert metrics.attempt_count == 3


def test_orchestrator_runner_populates_reliability_field_on_outcome() -> None:
    case = FrozenTaskCase(
        case_id="reliability-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"reliability-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    # Reliability should be populated by the orchestrator path.
    assert outcome.reliability is not None
    assert outcome.reliability.worker_status == "success"
    assert outcome.reliability.worker_failure_kind is None

"""Unit coverage for M25.6 evidence gates and sanitized reporting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evaluation.temporal_reliability_capture import load_suite
from evaluation.temporal_reliability_models import (
    BundleIdentity,
    CapturedCaseEvidence,
    OperatorAnnotations,
    TemporalActivityEvidence,
    TemporalHistoryEvidence,
)
from evaluation.temporal_reliability_report import (
    build_report,
    evaluate_case_gates,
    render_markdown,
    sanitize_case,
    validate_sanitized_payload,
)

SUITE = load_suite(Path("evaluation/m25_6_reliability_suite.json"))
IDENTITY = BundleIdentity(
    build_sha="abcdef123456",
    environment="staging",
    operator="operator",
    temporal_address="temporal:7233",
    temporal_namespace="default",
    database_url_env="DATABASE_URL",
)
ANNOTATIONS = OperatorAnnotations(
    manual_log_inspection=False,
    ci_rejection_count=0,
    review_rejection_count=0,
)


def _case(case_id: str):
    return next(case for case in SUITE.cases if case.case_id == case_id)


def _database(*, status: str = "completed", profile: str = "codex-native-executor") -> dict:
    created = datetime(2026, 8, 1, tzinfo=UTC)
    return {
        "task": {
            "id": "private",
            "status": status,
            "chosen_worker": "codex",
            "chosen_profile": profile,
            "runtime_mode": "native_agent",
            "orchestration_runtime": "temporal",
            "created_at": created.isoformat(),
            "control_constraints": {},
        },
        "worker_runs": [
            {
                "status": "success",
                "worker_type": "codex",
                "worker_profile": profile,
                "runtime_mode": "native_agent",
                "orchestration_runtime": "temporal",
                "runtime_manifest": {
                    "service": {"build_sha": IDENTITY.build_sha, "environment": "staging"},
                    "worker": {
                        "worker_type": "codex",
                        "worker_profile": profile,
                        "runtime_mode": "native_agent",
                    },
                    "task": {"read_only": profile.endswith("-read-only")},
                },
                "verifier_outcome": {
                    "status": "passed",
                    "summary": "private verification summary",
                },
                "artifact_index": [{"kind": "diff"}],
                "artifacts": [],
                "failure_kind": None,
                "delivery_metadata": {},
            }
        ],
        "timeline": [
            {
                "event_type": f"task_{status}",
                "created_at": (created + timedelta(seconds=10)).isoformat(),
            }
        ],
        "interactions": [],
        "execution_plan": None,
    }


def _history(*, status: str = "completed", events: int = 5) -> TemporalHistoryEvidence:
    return TemporalHistoryEvidence(
        workflow_id="task-private",
        run_id="run-private",
        workflow_status=status,
        event_count=events,
        history_sha256="a" * 64,
        activities=[
            TemporalActivityEvidence(
                activity_type="run_worker",
                scheduled_event_id=1,
                attempt=1,
                status="completed",
                latency_seconds=2,
            )
        ],
        activity_counts={"run_worker": 1},
        retry_activity_types=[],
        signal_names=[],
        fanout_overlap=False,
        raw_history_file="case.json",
    )


def test_gate_accepts_valid_mutation_and_rejects_identity_and_validation_drift() -> None:
    case = _case("mutation-codex-01")
    database = _database()
    assert (
        evaluate_case_gates(
            case=case,
            identity=IDENTITY,
            database=database,
            temporal=_history(),
            annotations=ANNOTATIONS,
        )
        == []
    )

    database["worker_runs"][0]["runtime_manifest"]["service"]["build_sha"] = "wrong"
    database["worker_runs"][0]["verifier_outcome"] = None
    failures = evaluate_case_gates(
        case=case,
        identity=IDENTITY,
        database=database,
        temporal=_history(),
        annotations=ANNOTATIONS,
    )

    assert "deployment.build_mismatch" in failures
    assert "proof.validation_missing" in failures


def test_gate_detects_expired_history_and_terminal_divergence() -> None:
    failures = evaluate_case_gates(
        case=_case("mutation-codex-01"),
        identity=IDENTITY,
        database=_database(status="failed"),
        temporal=_history(status="running", events=0),
        annotations=ANNOTATIONS,
    )

    assert "terminal.postgres_status_mismatch" in failures
    assert "terminal.temporal_status_mismatch" in failures
    assert "temporal.history_empty_or_expired" in failures
    assert "failure.kind_missing_or_unknown" in failures
    assert "failure.next_action_missing" in failures


def test_sanitized_projection_counts_metrics_without_private_content() -> None:
    database = _database()
    database["interactions"] = [
        {
            "interaction_type": "clarification",
            "status": "resolved",
            "data": {"questions": ["Which file?", " which   FILE? "]},
            "response_data": {"secret": "never publish"},
        },
        {
            "interaction_type": "clarification",
            "status": "pending",
            "data": {"questions": "A separate question"},
            "response_data": None,
        },
    ]
    capture = CapturedCaseEvidence(
        case_id="mutation-codex-01",
        task_id="private-task-id",
        expected=_case("mutation-codex-01"),
        database=database,
        temporal=_history(),
        annotations=ANNOTATIONS,
        gate_failures=[],
    )

    public_case = sanitize_case(capture)
    report = build_report(IDENTITY, [capture])
    markdown = render_markdown(report)

    assert public_case.repeated_clarification_questions == 1
    assert public_case.human_interventions == 1
    assert report.status == "incomplete"
    assert "private-task-id" not in markdown
    assert "private verification summary" not in markdown
    assert "never publish" not in markdown

    missing_time_database = {**database, "task": {**database["task"], "created_at": None}}
    missing_time_capture = capture.model_copy(update={"database": missing_time_database})
    assert sanitize_case(missing_time_capture).time_to_terminal_seconds is None
    assert build_report(IDENTITY, [capture] * 20).status == "invalid"


def test_public_payload_validator_rejects_private_fields() -> None:
    with pytest.raises(ValueError, match="forbidden public report field"):
        validate_sanitized_payload({"cases": [{"task_text": "private"}]})

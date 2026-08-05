"""Synthetic end-to-end coverage for the M25.6 evidence bundle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from db.base import Base
from db.enums import (
    ExecutionPlanNodeStatus,
    HumanInteractionStatus,
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import (
    ExecutionPlan,
    ExecutionPlanNode,
    ExecutionPlanNodeAttempt,
    HumanInteraction,
    Task,
    TaskTimelineEvent,
    WorkerRun,
)
from evaluation.temporal_history_evidence import canonical_json_bytes
from evaluation.temporal_reliability_capture import (
    HISTORIES_DIRECTORY,
    ensure_capture_available,
    initialize_bundle,
    load_bundle,
    load_captures,
    load_suite,
    persist_capture,
    persist_reused_capture,
    read_task_evidence,
)
from evaluation.temporal_reliability_models import (
    BundleIdentity,
    OperatorAnnotations,
    TemporalActivityEvidence,
    TemporalHistoryEvidence,
)
from evaluation.temporal_reliability_report import (
    build_report,
    evaluate_case_gates,
    render_markdown,
)
from repositories import create_engine_from_url
from scripts.e2e import run_temporal_reliability_eval as cli

SUITE_PATH = Path("evaluation/m25_6_reliability_suite.json")
BUILD_SHA = "abcdef1234567890"
ENVIRONMENT = "baseline"
STARTED_AT = datetime(2026, 8, 1, 10, tzinfo=UTC)


def _identity() -> BundleIdentity:
    return BundleIdentity(
        build_sha=BUILD_SHA,
        environment=ENVIRONMENT,
        operator="test-operator",
        temporal_address="temporal:7233",
        temporal_namespace="default",
        database_url_env="TEST_DATABASE_URL",
    )


def _provider(profile: str) -> WorkerType:
    return WorkerType.CODEX if profile.startswith("codex-") else WorkerType.ANTIGRAVITY


def _terminal_event(status: TaskStatus) -> TimelineEventType:
    return {
        TaskStatus.COMPLETED: TimelineEventType.TASK_COMPLETED,
        TaskStatus.FAILED: TimelineEventType.TASK_FAILED,
        TaskStatus.CANCELLED: TimelineEventType.TASK_CANCELLED,
    }[status]


def _add_plan(task: Task, *, fanout: bool) -> None:
    plan = ExecutionPlan(task=task)
    node_specs = (
        [("root", [], False), ("left", ["root"], True), ("right", ["root"], True)]
        if fanout
        else [("first", [], False), ("second", ["first"], False)]
    )
    for sequence, (node_id, dependencies, parallel_safe) in enumerate(node_specs):
        node = ExecutionPlanNode(
            execution_plan=plan,
            node_id=node_id,
            sequence_number=sequence,
            depends_on=dependencies,
            aggregation_role="analysis" if fanout else "mutation",
            execution_mode="read_only" if fanout else "mutable",
            parallel_safe=parallel_safe,
            status=ExecutionPlanNodeStatus.COMPLETED,
            goal=f"private goal {node_id}",
            verification_outcome={"status": "passed"},
        )
        node.attempts.append(
            ExecutionPlanNodeAttempt(
                attempt_number=1,
                started_at=STARTED_AT,
                finished_at=STARTED_AT + timedelta(seconds=1),
                duration_ms=1000,
                status="completed",
                effective_input_summary={"digest_only": True},
                effective_input_digest="a" * 64,
                logical_activity_key=f"{task.id}:{node_id}:1",
            )
        )


def _add_interaction(task: Task, proof: str) -> None:
    if proof == "clarification":
        task.human_interactions.append(
            HumanInteraction(
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.RESOLVED,
                summary="private clarification",
                data={"questions": ["Which file?", " which file? "]},
                response_data={"answer": "private"},
            )
        )
    elif proof == "permission_escalation":
        task.human_interactions.append(
            HumanInteraction(
                interaction_type=HumanInteractionType.PERMISSION,
                status=HumanInteractionStatus.RESOLVED,
                summary="private permission request",
                data={"permission": "network"},
                response_data={"approved": True},
            )
        )


def _task_for_case(case, sequence: int) -> Task:
    status = TaskStatus(case.expected_terminal_status)
    controls: dict[str, object] = {}
    if "verifier_repair" in case.required_proofs:
        controls["independent_verifier_repair_passes_used"] = 1
    if "independent_review_repair" in case.required_proofs:
        controls["independent_review_repair_passes_used"] = 1
    task = Task(
        session_id="synthetic-session",
        task_text=f"private task text {case.case_id}",
        repo_url=f"https://private.invalid/{case.case_id}",
        constraints=controls,
        status=status,
        chosen_worker=_provider(case.expected_profile),
        chosen_profile=case.expected_profile,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        created_at=STARTED_AT + timedelta(minutes=sequence),
        updated_at=STARTED_AT + timedelta(minutes=sequence, seconds=10),
    )
    run_status = (
        WorkerRunStatus.CANCELLED if status == TaskStatus.CANCELLED else WorkerRunStatus.SUCCESS
    )
    task.worker_runs.append(
        WorkerRun(
            worker_type=_provider(case.expected_profile),
            worker_profile=case.expected_profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            started_at=task.created_at + timedelta(seconds=1),
            finished_at=task.created_at + timedelta(seconds=8),
            status=run_status,
            summary="private worker summary",
            verifier_outcome={
                "status": "passed",
                "summary": "private verifier summary",
            },
            artifact_index=[{"kind": "result", "private_uri": "/tmp/private"}],
            runtime_manifest={
                "service": {"build_sha": BUILD_SHA, "environment": ENVIRONMENT},
                "worker": {
                    "worker_type": _provider(case.expected_profile).value,
                    "worker_profile": case.expected_profile,
                    "runtime_mode": "native_agent",
                },
                "task": {"read_only": case.expected_mode == "read_only"},
            },
            delivery_metadata=(
                {"pr_url": "https://github.invalid/private/1"}
                if "draft_pr" in case.required_proofs
                else {}
            ),
        )
    )
    timeline_types: list[TimelineEventType] = []
    if "approval" in case.required_proofs:
        timeline_types.append(TimelineEventType.APPROVAL_GRANTED)
    if "draft_pr" in case.required_proofs:
        timeline_types.append(TimelineEventType.DELIVERY_COMPLETED)
    timeline_types.append(_terminal_event(status))
    for event_sequence, event_type in enumerate(timeline_types):
        task.timeline_events.append(
            TaskTimelineEvent(
                attempt_number=0,
                sequence_number=event_sequence,
                event_type=event_type,
                created_at=task.created_at + timedelta(seconds=5 + event_sequence),
                message="private timeline message",
                payload={"private": True},
            )
        )
    if "sequential_dag" in case.required_proofs:
        _add_plan(task, fanout="fanout_overlap" in case.required_proofs)
    for proof in case.required_proofs:
        _add_interaction(task, proof)
    return task


def _history_for_case(case, raw_history: dict, *, task_id: str) -> TemporalHistoryEvidence:
    signals = [
        signal
        for proof, signal in {
            "approval": "handle_approval",
            "clarification": "handle_clarification",
            "permission_escalation": "handle_permission_escalation",
        }.items()
        if proof in case.required_proofs
    ]
    activity_type = (
        "run_decomposed_node" if "sequential_dag" in case.required_proofs else "run_worker"
    )
    activity_count = 2 if "sequential_dag" in case.required_proofs else 1
    if any("repair" in proof for proof in case.required_proofs):
        activity_count = 2
    if "fanout_overlap" in case.required_proofs:
        activity_count = 2
    activities = [
        TemporalActivityEvidence(
            activity_type=activity_type,
            scheduled_event_id=index + 1,
            attempt=2 if "worker_restart" in case.required_proofs else 1,
            status="completed",
            latency_seconds=float(index + 1),
        )
        for index in range(activity_count)
    ]
    return TemporalHistoryEvidence(
        workflow_id=f"task-{task_id}",
        run_id="synthetic-run",
        workflow_status="cancelled"
        if case.expected_terminal_status == "cancelled"
        else "completed",
        event_count=10,
        history_sha256=__import__("hashlib").sha256(canonical_json_bytes(raw_history)).hexdigest(),
        activities=activities,
        activity_counts={activity_type: activity_count},
        retry_activity_types=([activity_type] if "worker_restart" in case.required_proofs else []),
        signal_names=signals,
        fanout_overlap="fanout_overlap" in case.required_proofs,
        raw_history_file=f"{case.case_id}.json",
    )


def _build_database(tmp_path: Path) -> tuple[str, dict[str, str]]:
    database_path = tmp_path / "synthetic.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    engine = create_engine_from_url(database_url)
    Base.metadata.create_all(engine)
    suite = load_suite(SUITE_PATH)
    task_ids: dict[str, str] = {}
    with Session(engine) as session:
        for sequence, case in enumerate(suite.cases):
            task = _task_for_case(case, sequence)
            session.add(task)
            session.flush()
            task_ids[case.case_id] = task.id
        session.commit()
    engine.dispose()
    return database_url, task_ids


def _capture_suite(tmp_path: Path) -> tuple[Path, object, list]:
    suite = load_suite(SUITE_PATH)
    database_url, task_ids = _build_database(tmp_path)
    bundle_dir = tmp_path / "artifacts" / "m25-6"
    initialize_bundle(bundle_dir=bundle_dir, suite=suite, identity=_identity())
    for case in suite.cases:
        manifest, frozen_suite = load_bundle(bundle_dir)
        frozen_case = ensure_capture_available(
            manifest,
            frozen_suite,
            case_id=case.case_id,
            task_id=task_ids[case.case_id],
        )
        database = read_task_evidence(
            database_url=database_url,
            task_id=task_ids[case.case_id],
        )
        raw_history = {"case": case.case_id, "events": list(range(10))}
        raw_path = bundle_dir / HISTORIES_DIRECTORY / f"{case.case_id}.json"
        raw_path.write_bytes(canonical_json_bytes(raw_history) + b"\n")
        temporal = _history_for_case(case, raw_history, task_id=task_ids[case.case_id])
        annotations = OperatorAnnotations(
            manual_log_inspection=False,
            ci_rejection_count=0,
            review_rejection_count=0,
        )
        failures = evaluate_case_gates(
            case=case,
            identity=manifest.identity,
            database=database,
            temporal=temporal,
            annotations=annotations,
        )
        assert failures == [], case.case_id
        persist_capture(
            bundle_dir=bundle_dir,
            manifest=manifest,
            case=frozen_case,
            task_id=task_ids[case.case_id],
            database=database,
            temporal=temporal,
            annotations=annotations,
            gate_failures=failures,
        )
    manifest, _ = load_bundle(bundle_dir)
    return bundle_dir, manifest, load_captures(bundle_dir, manifest)


def test_synthetic_20_case_bundle_is_ready_and_sanitized(tmp_path: Path) -> None:
    _bundle_dir, manifest, captures = _capture_suite(tmp_path)

    report = build_report(manifest.identity, captures)
    markdown = render_markdown(report)
    public_json = report.model_dump_json()

    assert report.status == "ready_for_operator_review"
    assert report.captured_cases == 20
    assert report.valid_cases == 20
    assert report.metrics.validation_evidence_rate == 1
    assert report.metrics.repeated_clarification_questions == 1
    assert report.metrics.profile_success_rates["codex-native-executor"] < 1
    assert report.metrics.activity_stage_latency_seconds
    assert report.metrics.mean_time_to_terminal_seconds is not None
    assert report.metrics.mean_time_to_pr_seconds is not None
    for private_value in ("private task text", "private.invalid", "private worker summary"):
        assert private_value not in markdown
        assert private_value not in public_json


def test_bundle_rejects_duplicate_case_and_task(tmp_path: Path) -> None:
    bundle_dir, manifest, captures = _capture_suite(tmp_path)
    suite = load_suite(bundle_dir / "frozen_suite.json")

    with pytest.raises(ValueError, match="case already captured"):
        ensure_capture_available(
            manifest,
            suite,
            case_id=suite.cases[0].case_id,
            task_id="new-task",
        )
    uncaptured_manifest = manifest.model_copy(update={"capture_files": {}})
    with pytest.raises(ValueError, match="task already captured"):
        ensure_capture_available(
            uncaptured_manifest,
            suite,
            case_id=suite.cases[0].case_id,
            task_id=captures[0].task_id,
        )


def test_valid_capture_can_be_reused_with_source_identity_and_history(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_dir, source_manifest, source_captures = _capture_suite(source_root)
    suite = load_suite(SUITE_PATH)
    destination_dir = tmp_path / "destination"
    destination_identity = _identity().model_copy(update={"build_sha": "fedcba9876543210"})
    destination_manifest = initialize_bundle(
        bundle_dir=destination_dir,
        suite=suite,
        identity=destination_identity,
    )
    source_capture = source_captures[0]

    reused = persist_reused_capture(
        bundle_dir=destination_dir,
        manifest=destination_manifest,
        suite=suite,
        source_bundle_dir=source_dir,
        source_manifest=source_manifest,
        case_id=source_capture.case_id,
    )

    assert reused.source_identity == source_manifest.identity
    assert reused.gate_failures == []
    history_path = destination_dir / HISTORIES_DIRECTORY / reused.temporal.raw_history_file
    assert (
        history_path.read_bytes()
        == (
            source_dir / HISTORIES_DIRECTORY / source_capture.temporal.raw_history_file
        ).read_bytes()
    )
    refreshed_manifest, _ = load_bundle(destination_dir)
    assert refreshed_manifest.capture_files[reused.case_id].endswith(f"/{reused.case_id}.json")
    assert cli.main(["report", "--bundle-dir", str(destination_dir)]) == 2

    second_destination_dir = tmp_path / "second-destination"
    second_destination_manifest = initialize_bundle(
        bundle_dir=second_destination_dir,
        suite=suite,
        identity=_identity().model_copy(update={"build_sha": "0123456789abcdef"}),
    )
    reused_again = persist_reused_capture(
        bundle_dir=second_destination_dir,
        manifest=second_destination_manifest,
        suite=suite,
        source_bundle_dir=destination_dir,
        source_manifest=refreshed_manifest,
        case_id=source_capture.case_id,
    )

    assert reused_again.source_identity == source_manifest.identity
    assert cli.main(["report", "--bundle-dir", str(second_destination_dir)]) == 2


def test_bundle_rejects_unknown_case_and_modified_frozen_suite(tmp_path: Path) -> None:
    suite = load_suite(SUITE_PATH)
    bundle_dir = tmp_path / "private-bundle"
    manifest = initialize_bundle(bundle_dir=bundle_dir, suite=suite, identity=_identity())

    assert (bundle_dir / "manifest.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="unknown suite case"):
        ensure_capture_available(manifest, suite, case_id="not-in-suite", task_id="task")

    frozen_path = bundle_dir / "frozen_suite.json"
    payload = json.loads(frozen_path.read_text(encoding="utf-8"))
    payload["cases"][0], payload["cases"][1] = payload["cases"][1], payload["cases"][0]
    frozen_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen suite digest"):
        load_bundle(bundle_dir)


def test_read_task_evidence_rejects_missing_task(tmp_path: Path) -> None:
    database_url, _ = _build_database(tmp_path)

    with pytest.raises(ValueError, match="task not found"):
        read_task_evidence(database_url=database_url, task_id="missing")


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        (
            lambda database, temporal: database["task"].update(
                {"chosen_profile": "antigravity-native-executor"}
            ),
            "profile.task_mismatch",
        ),
        (
            lambda database, temporal: database["task"].update({"chosen_worker": "openrouter"}),
            "profile.task_worker_mismatch",
        ),
        (
            lambda database, temporal: database["task"].update({"orchestration_runtime": "legacy"}),
            "runtime.task_not_temporal",
        ),
        (
            lambda database, temporal: database["worker_runs"][0]["runtime_manifest"][
                "service"
            ].update({"build_sha": "wrong"}),
            "deployment.build_mismatch",
        ),
        (
            lambda database, temporal: database["worker_runs"][0].update(
                {"verifier_outcome": None}
            ),
            "proof.validation_missing",
        ),
        (
            lambda database, temporal: database["task"]["control_constraints"].update(
                {"independent_verifier_repair_passes_used": "invalid"}
            ),
            "proof.verifier_repair_state_missing",
        ),
        (
            lambda database, temporal: setattr(temporal, "event_count", 0),
            "temporal.history_empty_or_expired",
        ),
        (
            lambda database, temporal: database["timeline"].append(
                {"event_type": "task_failed", "created_at": STARTED_AT.isoformat()}
            ),
            "terminal.timeline_divergence",
        ),
    ],
)
def test_evidence_gates_reject_invalid_synthetic_state(
    tmp_path: Path, mutation, expected_failure: str
) -> None:
    database_url, task_ids = _build_database(tmp_path)
    target_case_id = (
        "mutation-verifier-repair"
        if expected_failure == "proof.verifier_repair_state_missing"
        else "mutation-codex-01"
    )
    case = next(case for case in load_suite(SUITE_PATH).cases if case.case_id == target_case_id)
    database = read_task_evidence(database_url=database_url, task_id=task_ids[case.case_id])
    temporal = _history_for_case(
        case,
        {"events": []},
        task_id=task_ids[case.case_id],
    )
    mutation(database, temporal)

    failures = evaluate_case_gates(
        case=case,
        identity=_identity(),
        database=database,
        temporal=temporal,
        annotations=OperatorAnnotations(
            manual_log_inspection=False,
            ci_rejection_count=0,
            review_rejection_count=0,
        ),
    )

    assert expected_failure in failures


@pytest.mark.parametrize(
    ("case_id", "mutation", "expected_failure"),
    [
        (
            "mutation-verifier-repair",
            lambda temporal: temporal.activity_counts.update({"run_worker": 1}),
            "proof.verifier_repair_activity_missing",
        ),
        (
            "mutation-independent-review-repair",
            lambda temporal: temporal.activity_counts.update({"run_worker": 1}),
            "proof.review_repair_activity_missing",
        ),
        (
            "sequential-dag-codex-01",
            lambda temporal: temporal.activity_counts.update({"run_decomposed_node": 1}),
            "proof.sequential_activity_missing",
        ),
        (
            "fanout-codex-01",
            lambda temporal: setattr(temporal, "fanout_overlap", False),
            "proof.fanout_overlap_missing",
        ),
        (
            "recovery-worker-restart",
            lambda temporal: setattr(temporal, "retry_activity_types", []),
            "proof.worker_restart_retry_missing",
        ),
    ],
)
def test_scenario_gates_require_temporal_sequence_proof(
    tmp_path: Path, case_id: str, mutation, expected_failure: str
) -> None:
    database_url, task_ids = _build_database(tmp_path)
    case = next(case for case in load_suite(SUITE_PATH).cases if case.case_id == case_id)
    task_id = task_ids[case.case_id]
    database = read_task_evidence(database_url=database_url, task_id=task_id)
    temporal = _history_for_case(case, {"events": []}, task_id=task_id)
    mutation(temporal)

    failures = evaluate_case_gates(
        case=case,
        identity=_identity(),
        database=database,
        temporal=temporal,
        annotations=OperatorAnnotations(
            manual_log_inspection=False,
            ci_rejection_count=0,
            review_rejection_count=0,
        ),
    )

    assert expected_failure in failures


def test_operator_annotations_are_complete_and_nonnegative() -> None:
    with pytest.raises(ValidationError):
        OperatorAnnotations.model_validate(
            {"manual_log_inspection": False, "review_rejection_count": 0}
        )
    with pytest.raises(ValidationError):
        OperatorAnnotations(
            manual_log_inspection=False,
            ci_rejection_count=-1,
            review_rejection_count=0,
        )


def test_cli_init_capture_and_incomplete_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url, task_ids = _build_database(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    monkeypatch.setenv("BUILD_SHA", BUILD_SHA)
    monkeypatch.setenv("TEST_DATABASE_URL", database_url)
    init_args = [
        "init",
        "--bundle-dir",
        str(bundle_dir),
        "--environment",
        ENVIRONMENT,
        "--operator",
        "test-operator",
        "--database-url-env",
        "TEST_DATABASE_URL",
        "--temporal-address",
        "temporal:7233",
    ]
    assert cli.main(init_args) == 0
    assert cli.main(init_args) == 1

    case = load_suite(SUITE_PATH).cases[0]

    async def fake_fetch_temporal_history(**kwargs):
        raw_history = {"events": [1]}
        kwargs["raw_history_path"].write_bytes(canonical_json_bytes(raw_history) + b"\n")
        return _history_for_case(case, raw_history, task_id=task_ids[case.case_id])

    monkeypatch.setattr(cli, "fetch_temporal_history", fake_fetch_temporal_history)
    capture_args = [
        "capture",
        "--bundle-dir",
        str(bundle_dir),
        "--case-id",
        case.case_id,
        "--task-id",
        task_ids[case.case_id],
        "--manual-log-inspection",
        "no",
        "--ci-rejection-count",
        "0",
        "--review-rejection-count",
        "0",
    ]
    assert cli.main(capture_args) == 0
    assert cli.main(capture_args) == 1
    assert cli.main(["report", "--bundle-dir", str(bundle_dir)]) == 2
    assert json.loads((bundle_dir / "report.json").read_text())["status"] == "incomplete"


def test_cli_reports_complete_bundle_ready(tmp_path: Path) -> None:
    bundle_dir, _manifest, _captures = _capture_suite(tmp_path)
    json_output = tmp_path / "public" / "baseline.json"
    markdown_output = tmp_path / "public" / "baseline.md"

    result = cli.main(
        [
            "report",
            "--bundle-dir",
            str(bundle_dir),
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert result == 0
    assert json.loads(json_output.read_text())["status"] == "ready_for_operator_review"
    assert "private task text" not in markdown_output.read_text()

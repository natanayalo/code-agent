"""Synthetic integration coverage for M29 canonical extractor-stage smokes."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import Base
from db.enums import ArtifactType, TaskStatus, TimelineEventType
from db.models import Task, WorkerRun
from evaluation.m29_mutation_smokes import (
    SmokeTaskRecord,
    default_smoke_policy,
    run_m29_extractor_stage_smokes,
    seed_mutation_extractor_smoke_database,
)
from evaluation.provider_reliability_extractor import load_task_evidence_snapshot
from repositories import create_engine_from_url


def _init_disposable_smoke_db(tmp_path: Path) -> tuple[str, list[SmokeTaskRecord]]:
    """Initialize a disposable SQLite database seeded with default mutation smoke tasks."""
    db_path = tmp_path / "m29_disposable_smokes.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        records = seed_mutation_extractor_smoke_database(session)

    engine.dispose()
    return db_url, records


def test_m29_extractor_stage_smokes_happy_path(tmp_path: Path) -> None:
    """Canonical extractor smoke verifies all invariants on representative disposable DB."""
    db_url, records = _init_disposable_smoke_db(tmp_path)
    result = run_m29_extractor_stage_smokes(db_url, records)

    assert result.is_valid is True
    assert result.violations == []
    assert result.total_smoke_tasks == 20
    assert result.candidate_profiles == (
        "codex-native-executor",
        "antigravity-native-executor",
    )
    assert result.all_verification_inapplicable is True
    assert result.all_review_inapplicable is True
    assert result.no_review_artifacts is True
    assert result.status_and_events_unchanged is True

    # Report verification and review stage pass rates must strictly be None
    assert result.report_verification_pass_rates["codex-native-executor"] is None
    assert result.report_review_pass_rates["codex-native-executor"] is None
    assert result.report_verification_pass_rates["antigravity-native-executor"] is None
    assert result.report_review_pass_rates["antigravity-native-executor"] is None


def test_m29_extractor_stage_smokes_per_case_invariants(tmp_path: Path) -> None:
    """Verify task evidence properties across ordinary and worker-reported-failed-tests paths."""
    db_url, records = _init_disposable_smoke_db(tmp_path)
    task_evidence, _ = load_task_evidence_snapshot(db_url, default_smoke_policy())

    assert len(task_evidence) == 20

    # Ensure all tasks are mutation feature tasks with verified identity
    for item in task_evidence:
        assert item.task_class == "feature"
        assert item.mutation_mode == "mutation"
        assert item.verification_applicable is False
        assert item.review_applicable is False
        assert item.verification_passed is False
        assert item.review_passed is False
        assert item.execution_identity_status == "verified"
        assert item.profile in ("codex-native-executor", "antigravity-native-executor")

    # Verify both path variants are represented and have verification_applicable=False
    ordinary_items = [item for item in task_evidence if "ord" in str(item.task_id)]
    shortcut_items = [item for item in task_evidence if "shrt" in str(item.task_id)]
    assert len(ordinary_items) == 10
    assert len(shortcut_items) == 10
    assert all(not it.verification_applicable for it in ordinary_items)
    assert all(not it.verification_applicable for it in shortcut_items)


def test_m29_extractor_stage_smokes_detects_review_artifact_violation(tmp_path: Path) -> None:
    """Smoke validator flags presence of INDEPENDENT_REVIEW_RESULT in worker runs."""
    db_url, records = _init_disposable_smoke_db(tmp_path)
    engine = create_engine_from_url(db_url)

    # Inject forbidden INDEPENDENT_REVIEW_RESULT artifact
    with Session(engine) as session:
        run = session.execute(select(WorkerRun)).scalars().first()
        assert run is not None
        run.artifact_index = [
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "uri": "https://reviews/forbidden.json",
            }
        ]
        session.commit()
    engine.dispose()

    result = run_m29_extractor_stage_smokes(db_url, records)
    assert result.is_valid is False
    assert result.no_review_artifacts is False
    assert any("contains INDEPENDENT_REVIEW_RESULT artifact" in v for v in result.violations)


def test_m29_extractor_stage_smokes_detects_status_and_event_mutations(tmp_path: Path) -> None:
    """Smoke validator flags any post-evaluation mutation of task status or terminal event."""
    db_url, records = _init_disposable_smoke_db(tmp_path)
    engine = create_engine_from_url(db_url)

    # Mutate status on one task and event on another
    with Session(engine) as session:
        t1 = session.get(Task, records[0].task_id)
        assert t1 is not None
        t1.status = TaskStatus.FAILED

        t2 = session.get(Task, records[1].task_id)
        assert t2 is not None
        t2.timeline_events[-1].event_type = TimelineEventType.TASK_FAILED
        session.commit()
    engine.dispose()

    result = run_m29_extractor_stage_smokes(db_url, records)
    assert result.is_valid is False
    assert result.status_and_events_unchanged is False
    assert any(f"task '{records[0].task_id}' status mutated" in v for v in result.violations)
    ev_msg = f"task '{records[1].task_id}' terminal event mutated"
    assert any(ev_msg in v for v in result.violations)


def test_m29_extractor_stage_smokes_detects_verification_applicable(tmp_path: Path) -> None:
    """Smoke validator flags tasks where verification was improperly made applicable."""
    db_url, records = _init_disposable_smoke_db(tmp_path)
    engine = create_engine_from_url(db_url)

    # Add verification commands to task_spec to make verification applicable
    with Session(engine) as session:
        t = session.get(Task, records[0].task_id)
        assert t is not None
        spec = dict(t.task_spec or {})
        spec["verification_commands"] = ["pytest tests/"]
        t.task_spec = spec
        session.commit()
    engine.dispose()

    result = run_m29_extractor_stage_smokes(db_url, records)
    assert result.is_valid is False
    assert result.all_verification_inapplicable is False
    expected_v_msg = f"task {records[0].task_id} has verification_applicable=True"
    assert any(expected_v_msg in v for v in result.violations)

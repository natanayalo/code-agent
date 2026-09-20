"""Coverage for the Wave 4 private-to-public manifest build path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.orm import Session

from db.base import Base
from db.enums import (
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import Task, TaskTimelineEvent, WorkerRun
from evaluation.m29_evidence_models import (
    M29BundleIdentity,
    M29CaseOutcome,
    M29EvidenceBundle,
    M29EvidenceSuite,
)
from evaluation.m29_wave4_paired import Wave4ManifestCase
from repositories import create_engine_from_url
from scripts.e2e import build_m29_wave4_manifest as builder


def _suite() -> tuple[Path, M29EvidenceSuite]:
    path = Path("evaluation/m29_live_provider_suite_wave4.json")
    return path, M29EvidenceSuite.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _write_bundle(path: Path, suite_path: Path, suite: M29EvidenceSuite) -> None:
    outcomes = {
        case.case_id: M29CaseOutcome(
            case_id=case.case_id,
            task_id=f"task-{index}",
            task_class=case.task_class,
            worker_profile=case.worker_profile,
            terminal_status="completed",
            created_at="2026-09-20T00:00:00+00:00",
            terminal_at="2026-09-20T00:01:00+00:00",
        )
        for index, case in enumerate(suite.cases)
    }
    bundle = M29EvidenceBundle(
        identity=M29BundleIdentity(
            build_sha="b" * 40,
            target_repository_revision="c" * 40,
            environment="test",
            operator="pytest",
        ),
        suite_sha256=builder._sha256(suite_path),
        cases=outcomes,
    )
    (path / "bundle.json").write_text(json.dumps(bundle.model_dump(mode="json")), encoding="utf-8")


def _worker_run(provider: str, profile: str, started_at, finished_at) -> WorkerRun:
    return WorkerRun(
        worker_type=WorkerType(provider),
        worker_profile=profile,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        started_at=started_at,
        finished_at=finished_at,
        status=WorkerRunStatus.SUCCESS,
        budget_usage={
            "native_agent": {
                "model_execution": {
                    "provider": provider,
                    "model": "gpt-5.6-luna" if provider == "codex" else "gemini-3.8-flash",
                    "reasoning_effort": "high" if provider == "codex" else "medium",
                }
            }
        },
    )


def _seed_database(path: Path, suite: M29EvidenceSuite) -> str:
    database_url = f"sqlite+pysqlite:///{path / 'evidence.db'}"
    engine = create_engine_from_url(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for index, case in enumerate(suite.cases):
            started_at = builder._parse_as_of("2026-09-20T00:00:00+00:00")
            finished_at = builder._parse_as_of("2026-09-20T00:01:00+00:00")
            provider = "codex" if case.worker_profile.startswith("codex-") else "antigravity"
            task = Task(
                id=f"task-{index}",
                session_id="synthetic-session",
                task_text=case.prompt,
                status=TaskStatus.COMPLETED,
                constraints={"read_only": True},
                task_spec={"task_type": "investigation", "allowed_actions": []},
                budget={},
                chosen_worker=WorkerType(provider),
                chosen_profile=case.worker_profile,
                runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                orchestration_runtime=OrchestrationRuntime.TEMPORAL,
                created_at=started_at,
                updated_at=finished_at,
            )
            task.worker_runs.append(
                _worker_run(provider, case.worker_profile, started_at, finished_at)
            )
            task.timeline_events.extend(
                [
                    TaskTimelineEvent(
                        attempt_number=0,
                        sequence_number=0,
                        event_type=TimelineEventType.TASK_INGESTED,
                        created_at=started_at,
                    ),
                    TaskTimelineEvent(
                        attempt_number=0,
                        sequence_number=1,
                        event_type=TimelineEventType.TASK_COMPLETED,
                        created_at=finished_at,
                    ),
                ]
            )
            session.add(task)
        session.commit()
    engine.dispose()
    return database_url


def test_build_manifest_promotes_wave3_cases_to_wave4_models(tmp_path: Path, monkeypatch) -> None:
    """Exercise build_manifest with a complete twenty-case private bundle."""
    suite_path, suite = _suite()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _write_bundle(bundle_dir, suite_path, suite)
    reports = {
        name: tmp_path / f"{name}.json"
        for name in ("baseline", "advisory", "operational", "robustness")
    }
    for path in reports.values():
        path.write_text("{}", encoding="utf-8")

    class FakeReport:
        @staticmethod
        def model_validate(payload):
            return SimpleNamespace()

    monkeypatch.setenv("TEST_DATABASE_URL", _seed_database(tmp_path, suite))
    monkeypatch.setattr(builder, "ProviderReliabilityReport", FakeReport)
    monkeypatch.setattr(builder, "assert_sanitized_wave4_manifest", lambda payload: None)
    monkeypatch.setattr(
        builder, "assert_wave4_manifest_matches_report", lambda *args, **kwargs: None
    )
    args = SimpleNamespace(
        suite=suite_path,
        bundle_dir=bundle_dir,
        as_of="2026-09-20T00:01:00+00:00",
        database_url_env="TEST_DATABASE_URL",
        baseline_report=reports["baseline"],
        advisory_report=reports["advisory"],
        operational_report=reports["operational"],
        robustness_report=reports["robustness"],
    )

    manifest = builder.build_manifest(args)

    assert len(manifest.cases) == 20
    assert all(isinstance(case, Wave4ManifestCase) for case in manifest.cases)

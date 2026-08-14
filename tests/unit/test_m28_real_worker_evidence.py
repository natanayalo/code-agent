"""Unit coverage for the M28 real-worker evidence contract."""
# ruff: noqa: E501

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evaluation.m28_real_worker_evidence import (
    build_report,
    evaluate_pair,
    initialize_bundle,
    load_suite,
    persist_pair,
    validate_public_payload,
    write_public_report,
)
from evaluation.m28_real_worker_models import BundleIdentity, PairMeasurements, PrivatePairCapture
from scripts.e2e.run_m28_real_worker_eval import (
    _assert_assisted_memory_delivery,
    _assert_fixture_safe,
    _command_markers_from_artifacts,
    _compact_session_context,
    _external_thread_id,
    _fixture_source,
    _task_text,
)


def _measurement(
    *, markers: list[str] | None = None, suppressed: list[str] | None = None
) -> PairMeasurements:
    return PairMeasurements(
        terminal_status="completed",
        accepted_reason_codes=["accepted_low_risk_fresh"],
        command_markers=markers or [],
        suppressed_keys=suppressed or [],
        questions=0,
        interventions=0,
        session_continuity=True,
    )


def _capture(case_id: str, scenario: str, profile: str) -> PrivatePairCapture:
    marker = f"m28-{scenario}-marker"
    assisted = _measurement(markers=[marker])
    if scenario == "irrelevant_rejection":
        assisted = _measurement()
    if scenario == "stale_reverification":
        assisted = _measurement(suppressed=["m28-stale"])
    if scenario == "conflict_handling":
        assisted = _measurement(markers=[marker], suppressed=["m28-conflict-personal"])
    return PrivatePairCapture(
        case_id=case_id,
        scenario=scenario,
        worker_profile=profile,
        cold_task_id="private-cold-id",
        assisted_task_id="private-assisted-id",
        cold=_measurement(),
        assisted=assisted,
    )


def test_suite_has_eight_provider_scenario_pairs() -> None:
    suite = load_suite()
    assert len(suite.cases) == 8


def test_useful_gate_requires_command_marker() -> None:
    capture = _capture("useful-hit-codex", "useful_hit", "codex-native-executor-read-only")
    capture.assisted.command_markers = []
    assert "useful_memory_not_used" in evaluate_pair(capture)


def test_stale_and_conflict_gates_fail_closed() -> None:
    stale = _capture(
        "stale-reverification-codex", "stale_reverification", "codex-native-executor-read-only"
    )
    stale.assisted.suppressed_keys = []
    conflict = _capture(
        "conflict-handling-codex", "conflict_handling", "codex-native-executor-read-only"
    )
    conflict.assisted.command_markers = []
    assert "stale_memory_not_suppressed" in evaluate_pair(stale)
    assert "project_memory_not_used" in evaluate_pair(conflict)


def test_assisted_capture_requires_native_memory_delivery_receipt() -> None:
    case = load_suite().cases[0]
    task = {
        "latest_run": {
            "budget_usage": {
                "native_agent": {
                    "memory_delivery": {
                        "delivered_memory_keys": ["m28-useful-hit-codex"],
                        "missing_accepted_memory_keys": [],
                        "complete": True,
                    }
                }
            }
        }
    }

    _assert_assisted_memory_delivery(task, case)
    task["latest_run"]["budget_usage"]["native_agent"]["memory_delivery"]["complete"] = False
    with pytest.raises(ValueError, match="not delivered"):
        _assert_assisted_memory_delivery(task, case)


def test_complete_valid_matrix_renders_effective_allowlisted_report(tmp_path: Path) -> None:
    suite = load_suite()
    bundle_dir = tmp_path / "private"
    initialize_bundle(
        bundle_dir,
        suite=suite,
        identity=BundleIdentity(
            build_sha="a" * 40, environment="test", repository_revision="rev", operator="tester"
        ),
    )
    for case in suite.cases:
        persist_pair(bundle_dir, _capture(case.case_id, case.scenario, case.worker_profile))

    report = build_report(bundle_dir)
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    write_public_report(report, json_path, markdown_path)

    assert report.conclusion == "effective"
    payload = json.loads(json_path.read_text())
    assert "private-cold-id" not in json_path.read_text()
    assert payload["captured_pairs"] == 8
    assert markdown_path.read_text().endswith("\n")


def test_bundle_refuses_duplicate_capture_and_public_private_fields(tmp_path: Path) -> None:
    suite = load_suite()
    bundle_dir = tmp_path / "private"
    initialize_bundle(
        bundle_dir,
        suite=suite,
        identity=BundleIdentity(
            build_sha="b" * 40, environment="test", repository_revision="rev", operator="tester"
        ),
    )
    case = suite.cases[0]
    capture = _capture(case.case_id, case.scenario, case.worker_profile)
    persist_pair(bundle_dir, capture)
    with pytest.raises(ValueError, match="already captured"):
        persist_pair(bundle_dir, capture)
    with pytest.raises(ValueError, match="forbidden public field"):
        validate_public_payload({"task_id": "private"})


def test_cli_refuses_initialization_without_disposable_stack_acknowledgement(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e/run_m28_real_worker_eval.py",
            "init",
            "--bundle-dir",
            str(tmp_path / "private"),
            "--build-sha",
            "c" * 40,
            "--environment",
            "test",
            "--repository-revision",
            "revision",
            "--operator",
            "tester",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "--ack-disposable-stack" in result.stderr


def test_cli_requires_disposable_repository_url_for_a_pair(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e/run_m28_real_worker_eval.py",
            "run-pair",
            "--bundle-dir",
            str(tmp_path / "private"),
            "--case-id",
            "useful-hit-codex",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--repo-url" in result.stderr


def test_private_artifacts_prove_markers_and_session_comparison_is_compact(
    tmp_path: Path,
) -> None:
    event_log = tmp_path / "events.jsonl"
    event_log.write_text('{"command":"printf m28-useful_hit-marker"}\n')
    task = {
        "latest_run": {
            "artifacts": [
                {"name": "native-agent-stdout", "uri": event_log.as_uri()},
                {"name": "workspace", "uri": (tmp_path / "workspace").as_uri()},
            ]
        }
    }

    assert _command_markers_from_artifacts(task) == ["m28-useful_hit-marker"]
    assert _compact_session_context(
        {
            "active_goal": "goal",
            "decisions_made": {"choice": "read-only"},
            "identified_risks": {},
            "files_touched": ["one.py"],
            "updated_at": "not-part-of-compact-context",
        }
    ) == {
        "active_goal": "goal",
        "decisions_made": {"choice": "read-only"},
        "identified_risks": {},
        "files_touched": ["one.py"],
    }


def test_bundle_scoped_thread_and_fixture_ownership_protect_cold_runs() -> None:
    case = load_suite().cases[0]
    first_source = _fixture_source("bundle-one", case)
    second_source = _fixture_source("bundle-two", case)
    assert _external_thread_id("bundle-one", case) != _external_thread_id("bundle-two", case)

    class _Response:
        def __init__(self, source: str) -> None:
            self.source = source

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, str]]:
            return [{"memory_key": "m28-useful-hit-codex", "source": self.source}]

    class _Client:
        def __init__(self, source: str) -> None:
            self.source = source

        def get(self, *_args: object, **_kwargs: object) -> _Response:
            return _Response(self.source)

    payload = {"memory_key": "m28-useful-hit-codex"}
    _assert_fixture_safe(
        _Client(first_source), "project", payload, "https://example.test/repo", first_source
    )
    with pytest.raises(ValueError, match="another bundle"):
        _assert_fixture_safe(
            _Client(second_source), "project", payload, "https://example.test/repo", first_source
        )


def test_only_useful_and_conflict_tasks_request_an_accepted_memory_command() -> None:
    useful = _capture("useful-hit-codex", "useful_hit", "codex-native-executor-read-only")
    stale = _capture(
        "stale-reverification-codex", "stale_reverification", "codex-native-executor-read-only"
    )

    assert "succeeds only by executing" in _task_text(useful)
    assert "stale" in _task_text(stale)
    assert "succeeds only by executing" not in _task_text(stale)

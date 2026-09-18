"""Integration tests for the M29 evidence wave operator CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from evaluation.m29_evidence_models import M29CaseOutcome, M29EvidenceBundle
from scripts.e2e.run_m29_evidence_wave import main

SUITE_PATH = Path("evaluation/m29_live_provider_suite.json")


@pytest.fixture
def temp_bundle_dir(tmp_path: Path) -> Path:
    bundle_dir = tmp_path / "test_bundle"
    return bundle_dir


def test_cli_init_requires_acknowledgment(temp_bundle_dir: Path) -> None:
    code = main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )
    assert code == 1
    assert not (temp_bundle_dir / "bundle.json").exists()


def test_cli_init_success_and_duplicate_prevention(temp_bundle_dir: Path) -> None:
    code = main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--ack-live-read-only-evidence",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )
    assert code == 0
    assert (temp_bundle_dir / "bundle.json").exists()

    raw = json.loads((temp_bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    bundle = M29EvidenceBundle.model_validate(raw)
    assert bundle.identity.build_sha == "1234567890abcdef1234567890abcdef12345678"
    assert bundle.identity.target_repository_revision == "abcdef1234567890abcdef1234567890abcdef12"
    assert bundle.cases == {}
    assert bundle.in_flight == {}

    # Duplicate init must fail
    code_dup = main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--ack-live-read-only-evidence",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )
    assert code_dup == 1


def test_cli_status(temp_bundle_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--ack-live-read-only-evidence",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )

    code = main(
        [
            "status",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--suite-path",
            str(SUITE_PATH),
        ]
    )
    assert code == 0
    captured = capsys.readouterr().out
    assert "0/28 completed" in captured
    assert "Pending Cases:" in captured


@patch("scripts.e2e.run_m29_evidence_wave._client")
def test_cli_run_batch_preflight_failure(
    mock_client_factory: MagicMock, temp_bundle_dir: Path
) -> None:
    main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--ack-live-read-only-evidence",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.HTTPError("Connection failed")
    mock_client_factory.return_value = mock_client

    with pytest.raises(RuntimeError, match="preflight /health check failed"):
        main(
            [
                "run-batch",
                "--bundle-dir",
                str(temp_bundle_dir),
                "--suite-path",
                str(SUITE_PATH),
            ]
        )


def _create_mock_batch_client() -> MagicMock:
    """Construct mock HTTP client simulating healthy stack and successful task execution."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client

    mock_ok = MagicMock()
    mock_ok.status_code = 200
    mock_ok.raise_for_status.return_value = None

    def mock_get(url: str, **kwargs):
        if url in ("/health", "/ready"):
            return mock_ok
        if url.startswith("/tasks/in-flight-task-2"):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status.return_value = None
            resp.json.return_value = {
                "task_id": "in-flight-task-2",
                "status": "completed",
                "orchestration_runtime": "temporal",
                "runtime_mode": "native_agent",
                "task_spec": {"task_type": "investigation", "delivery_mode": "summary"},
                "chosen_profile": "antigravity-native-executor-read-only",
                "latest_run": {"files_changed": []},
            }
            return resp
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        task_id = url.split("/")[-1]
        resp.json.return_value = {
            "task_id": task_id,
            "status": "completed",
            "orchestration_runtime": "temporal",
            "runtime_mode": "native_agent",
            "task_spec": {
                "task_type": "feature" if "feat" in task_id else "docs",
                "delivery_mode": "summary",
            },
            "chosen_profile": (
                "codex-native-executor-read-only"
                if "codex" in task_id
                else "antigravity-native-executor-read-only"
            ),
            "latest_run": {"files_changed": []},
        }
        return resp

    def mock_post(url: str, **kwargs):
        payload = kwargs.get("json", {})
        profile = payload.get("worker_profile_override", "")
        task_text = payload.get("task_text", "")
        tag = "codex" if "codex" in profile else "antigravity"
        kind = "feat" if "proposal" in task_text else "docs"
        task_id = f"task-sub-{kind}-{tag}-{abs(hash(task_text)) % 10000}"
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"task_id": task_id}
        return resp

    mock_client.get.side_effect = mock_get
    mock_client.post.side_effect = mock_post
    return mock_client


@patch("scripts.e2e.run_m29_evidence_wave._client")
def test_cli_run_batch_resumption_and_idempotency(
    mock_client_factory: MagicMock, temp_bundle_dir: Path
) -> None:
    main(
        [
            "init",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--build-sha",
            "1234567890abcdef1234567890abcdef12345678",
            "--target-repository-revision",
            "abcdef1234567890abcdef1234567890abcdef12",
            "--ack-live-read-only-evidence",
            "--suite-path",
            str(SUITE_PATH),
        ]
    )

    bundle_path = temp_bundle_dir / "bundle.json"
    bundle = M29EvidenceBundle.model_validate(json.loads(bundle_path.read_text()))
    bundle.cases["m29-inv-01-extractor-timeline"] = M29CaseOutcome(
        case_id="m29-inv-01-extractor-timeline",
        task_id="pre-completed-task-1",
        task_class="investigation",
        worker_profile="antigravity-native-executor-read-only",
        terminal_status="completed",
        files_changed_count=0,
        time_to_terminal_seconds=30.0,
        created_at="2026-09-17T20:00:00Z",
        terminal_at="2026-09-17T20:00:30Z",
    )
    bundle.in_flight["m29-inv-02-robustness-boundaries"] = "in-flight-task-2"
    bundle_path.write_text(json.dumps(bundle.model_dump(mode="json"), indent=2))

    mock_client_factory.return_value = _create_mock_batch_client()

    code = main(
        [
            "run-batch",
            "--bundle-dir",
            str(temp_bundle_dir),
            "--suite-path",
            str(SUITE_PATH),
        ]
    )
    assert code == 0

    updated_bundle = M29EvidenceBundle.model_validate(json.loads(bundle_path.read_text()))
    assert len(updated_bundle.cases) == 28
    assert updated_bundle.in_flight == {}
    assert updated_bundle.cases["m29-inv-01-extractor-timeline"].task_id == "pre-completed-task-1"
    assert updated_bundle.cases["m29-inv-02-robustness-boundaries"].task_id == "in-flight-task-2"

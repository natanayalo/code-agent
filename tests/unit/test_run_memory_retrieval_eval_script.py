"""Tests for the memory retrieval evaluation script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_suite(path: Path, *, known_gap: bool = False, regression: bool = False) -> None:
    case = {
        "case_id": "case-1",
        "task_text": "pytest" if not regression else "missing",
        "expected_project_keys": ["pytest_matrix"],
    }
    if known_gap:
        case = {
            "case_id": "case-1",
            "task_text": "semantic miss",
            "expected_project_keys": ["pytest_matrix"],
            "known_semantic_gap_project_keys": ["pytest_matrix"],
        }
    path.write_text(
        json.dumps(
            {
                "suite_name": "script-memory-eval",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "personal_memory": [],
                "project_memory": [
                    {
                        "memory_key": "pytest_matrix",
                        "value": {"cmd": ".venv/bin/pytest", "purpose": "pytest"},
                    }
                ],
                "cases": [case],
            }
        ),
        encoding="utf-8",
    )


def _run_script(
    *,
    suite_path: Path,
    output_path: Path,
    fail_under_recall: float | None = None,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "e2e" / "run_memory_retrieval_eval.py"
    command = [
        sys.executable,
        str(script_path),
        "--suite",
        str(suite_path),
        "--output",
        str(output_path),
    ]
    if fail_under_recall is not None:
        command.extend(["--fail-under-recall", str(fail_under_recall)])
    return subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_memory_retrieval_eval_exits_zero_and_writes_report(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)

    result = _run_script(suite_path=suite_path, output_path=output_path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert payload["recall"] == 1.0
    assert payload["regression_misses"] == []
    assert output_path.read_text(encoding="utf-8").endswith("\n")


def test_run_memory_retrieval_eval_fail_under_recall_ignores_known_gaps(
    tmp_path: Path,
) -> None:
    suite_path = tmp_path / "suite.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path, known_gap=True)

    result = _run_script(
        suite_path=suite_path,
        output_path=output_path,
        fail_under_recall=1.0,
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert payload["recall"] is None
    assert payload["known_semantic_gap_misses"] == ["case-1:project:pytest_matrix"]


def test_run_memory_retrieval_eval_fail_under_recall_exits_nonzero_for_regression(
    tmp_path: Path,
) -> None:
    suite_path = tmp_path / "suite.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path, regression=True)

    result = _run_script(
        suite_path=suite_path,
        output_path=output_path,
        fail_under_recall=1.0,
    )

    assert result.returncode == 1
    assert output_path.exists()

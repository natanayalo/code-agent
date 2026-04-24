"""Tests for frozen evaluation script exit codes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_suite(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "suite_name": "script-exit-code",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"require_success": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _run_script(
    *,
    suite_path: Path,
    replay_path: Path,
    output_path: Path,
    parallel: bool = False,
    max_parallel_cases: int | None = None,
    variant_label: str | None = None,
    review_prompt_profile: str | None = None,
    reviewer_model_profile: str | None = None,
    compare_to_report: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "e2e" / "run_frozen_eval.py"
    command = [
        sys.executable,
        str(script_path),
        "--runner",
        "replay",
        "--suite",
        str(suite_path),
        "--replay",
        str(replay_path),
        "--output",
        str(output_path),
    ]
    if parallel:
        command.append("--parallel")
    if max_parallel_cases is not None:
        command.extend(["--max-parallel-cases", str(max_parallel_cases)])
    if variant_label is not None:
        command.extend(["--variant-label", variant_label])
    if review_prompt_profile is not None:
        command.extend(["--review-prompt-profile", review_prompt_profile])
    if reviewer_model_profile is not None:
        command.extend(["--reviewer-model-profile", reviewer_model_profile])
    if compare_to_report is not None:
        command.extend(["--compare-to-report", str(compare_to_report)])
    return subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_frozen_eval_exits_zero_when_all_cases_pass(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "success", "summary": "ok"}}),
        encoding="utf-8",
    )

    result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
    )

    assert result.returncode == 0
    assert output_path.exists()


def test_run_frozen_eval_exits_non_zero_when_any_case_fails(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "failure", "summary": "not ok"}}),
        encoding="utf-8",
    )

    result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
    )

    assert result.returncode == 1
    assert output_path.exists()


def test_run_frozen_eval_supports_parallel_flag(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "success", "summary": "ok"}}),
        encoding="utf-8",
    )

    result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
        parallel=True,
    )

    assert result.returncode == 0
    assert output_path.exists()


def test_run_frozen_eval_supports_parallel_limit_flag(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "success", "summary": "ok"}}),
        encoding="utf-8",
    )

    result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
        parallel=True,
        max_parallel_cases=1,
    )

    assert result.returncode == 0
    assert output_path.exists()


def test_run_frozen_eval_persists_variant_profile_metadata(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    output_path = tmp_path / "report.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "success", "summary": "ok"}}),
        encoding="utf-8",
    )

    result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
        variant_label="candidate",
        review_prompt_profile="review-prompt-v2",
        reviewer_model_profile="gpt-reviewer",
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    profile = payload["profile"]

    assert result.returncode == 0
    assert profile["variant_label"] == "candidate"
    assert profile["review_prompt_profile"] == "review-prompt-v2"
    assert profile["reviewer_model_profile"] == "gpt-reviewer"


def test_run_frozen_eval_supports_compare_to_report(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    replay_path = tmp_path / "replay.json"
    baseline_output_path = tmp_path / "baseline.json"
    output_path = tmp_path / "candidate.json"
    _write_suite(suite_path)
    replay_path.write_text(
        json.dumps({"case-1": {"status": "success", "summary": "ok"}}),
        encoding="utf-8",
    )

    baseline_result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=baseline_output_path,
        variant_label="baseline",
    )
    candidate_result = _run_script(
        suite_path=suite_path,
        replay_path=replay_path,
        output_path=output_path,
        variant_label="candidate",
        compare_to_report=baseline_output_path,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert baseline_result.returncode == 0
    assert candidate_result.returncode == 0
    assert payload["comparison"]["baseline_variant_label"] == "baseline"
    assert payload["comparison"]["candidate_variant_label"] == "candidate"

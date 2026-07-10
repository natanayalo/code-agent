"""Unit and integration tests for the behavior reliability evaluation script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from scripts.e2e.behavior_reliability_support import load_dotenv, parse_env_value, setup_dummy_repo
from scripts.e2e.run_behavior_reliability_eval import (
    CaseResult,
    ContractRunner,
    _write_report,
    execute_eval_cleanup,
    parse_args,
    parse_env_map,
)


def test_parse_args_defaults() -> None:
    """Verify default CLI arguments parsed correctly."""
    test_args = ["--mode", "contract"]
    with patch.object(sys, "argv", ["run_behavior_reliability_eval.py"] + test_args):
        args = parse_args()
        assert args.mode == "contract"
        assert args.base_url == "http://localhost:8000"
        assert args.timeout_seconds == 180
        assert args.poll_interval_seconds == 2.0
        assert args.output == "artifacts/evaluations/behavior-reliability-report.json"
        assert args.keep_temp_repo is False
        assert args.skip_cleanup is False
        assert args.case is None
        assert args.run_id is None
        assert args.repo_root is None


def test_parse_args_overrides() -> None:
    """Verify custom CLI argument overrides."""
    test_args = [
        "--mode",
        "live",
        "--base-url",
        "https://agent.test",
        "--timeout-seconds",
        "60",
        "--poll-interval-seconds",
        "5",
        "--output",
        "tmp/custom_report.json",
        "--keep-temp-repo",
        "--skip-cleanup",
        "--case",
        "stale_policy_avoidance",
        "--run-id",
        "eval-123",
        "--repo-root",
        "/tmp/custom_repo",
    ]
    with patch.object(sys, "argv", ["run_behavior_reliability_eval.py"] + test_args):
        args = parse_args()
        assert args.mode == "live"
        assert args.base_url == "https://agent.test"
        assert args.timeout_seconds == 60
        assert args.poll_interval_seconds == 5
        assert args.output == "tmp/custom_report.json"
        assert args.keep_temp_repo is True
        assert args.skip_cleanup is True
        assert args.case == "stale_policy_avoidance"
        assert args.run_id == "eval-123"
        assert args.repo_root == "/tmp/custom_repo"


def test_execute_eval_cleanup_handles_errors() -> None:
    """Verify execute_eval_cleanup accumulates errors when delete functions fail."""
    mock_runner = MagicMock()
    mock_runner.delete_project.side_effect = Exception("db error project")
    mock_runner.delete_personal.side_effect = Exception("db error personal")

    errors = execute_eval_cleanup(mock_runner, ["key1", "key2"])
    assert len(errors) == 4
    assert any("Failed deleting project memory key1" in err for err in errors)
    assert any("Failed deleting personal memory key2" in err for err in errors)
    assert mock_runner.delete_project.call_count == 2
    assert mock_runner.delete_personal.call_count == 2


def test_setup_dummy_repo_refuses_unmarked_existing_directory(tmp_path: Path) -> None:
    """The evaluator must not delete a repository it did not create."""
    repo_dir = tmp_path / "existing-repo"
    repo_dir.mkdir()
    (repo_dir / "important.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ValueError, match="unmarked existing directory"):
        setup_dummy_repo(str(repo_dir))

    assert (repo_dir / "important.txt").read_text(encoding="utf-8") == "keep me"


def test_load_dotenv_strips_inline_comments_before_quotes(tmp_path: Path, monkeypatch) -> None:
    """Quoted dotenv values with comments should load without the comment."""
    env_path = tmp_path / ".env"
    env_path.write_text('EVAL_QUOTED="value" # comment\n', encoding="utf-8")
    monkeypatch.delenv("EVAL_QUOTED", raising=False)

    load_dotenv(str(env_path))

    assert os.environ["EVAL_QUOTED"] == "value"


def test_parse_env_value_preserves_hashes_inside_quotes() -> None:
    """Quoted hashes remain part of the value before an inline comment."""
    assert parse_env_value('"value#with#hash" # comment') == "value#with#hash"


def test_parse_env_map_preserves_first_repo_mapping(monkeypatch) -> None:
    """Repository map parsing should preserve the first configured value."""
    monkeypatch.setenv("EVAL_REPOS", "qa-dummy:first,qa-dummy:second,other:value")

    assert parse_env_map("EVAL_REPOS") == {"qa-dummy": "first", "other": "value"}


def test_write_report_supports_filename_without_directory(tmp_path: Path, monkeypatch) -> None:
    """A report filename without a directory should be written successfully."""
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(output="report.json", base_url="http://localhost:8000", mode="contract")

    _write_report(args, "run-1", "2026-07-10T00:00:00Z", [CaseResult("case")], [], False)

    assert (tmp_path / "report.json").exists()


def test_contract_runner_supports_memory_database_query_parameters(monkeypatch) -> None:
    """In-memory SQLite remains shared when the URL includes query parameters."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:?cache=shared")

    runner = ContractRunner(run_id="run-1", repo_url="file:///tmp/dummy_repo")
    runner.seed_project(key="test", value={"ok": True}, requires_verification=False)


def test_contract_execution_runs_successfully(tmp_path: Path) -> None:
    """Verify that running the evaluation script in contract mode produces a successful report."""
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "e2e" / "run_behavior_reliability_eval.py"
    report_output_path = tmp_path / "behavior-report.json"

    # Set temporary workspace root env var to avoid hitting production databases/workspaces
    env = os.environ.copy()
    env["CODE_AGENT_WORKSPACE_ROOT"] = str(tmp_path / "workspaces")
    env["DATABASE_URL"] = f"sqlite:///{env['CODE_AGENT_WORKSPACE_ROOT']}/test.db"
    os.makedirs(env["CODE_AGENT_WORKSPACE_ROOT"], exist_ok=True)

    command = [
        sys.executable,
        str(script_path),
        "--mode",
        "contract",
        "--output",
        str(report_output_path),
    ]

    result = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, (
        f"Script failed with stdout:\n{result.stdout}\n" f"stderr:\n{result.stderr}"
    )
    assert report_output_path.exists()

    with open(report_output_path, encoding="utf-8") as f:
        report = json.load(f)

    assert report["mode"] == "contract"
    assert report["passed"] is True
    assert len(report["cases"]) == 3

    case_ids = {c["case_id"] for c in report["cases"]}
    assert case_ids == {
        "profile_command_injected_and_used",
        "stale_policy_avoidance",
        "unsafe_action_protection",
    }

    for case in report["cases"]:
        assert case["passed"] is True
        for assertion in case["assertions"]:
            assert assertion["passed"] is True

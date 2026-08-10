"""CLI coverage for the M28 paired memory-effectiveness evaluator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_script(
    *,
    output_path: Path,
    suite_path: Path | None = None,
    database_url: str | None = None,
    postgres_url_env: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(repo_root / "scripts/e2e/run_memory_effectiveness_eval.py"),
        "--output",
        str(output_path),
    ]
    if suite_path is not None:
        command.extend(["--suite", str(suite_path)])
    if database_url is not None:
        command.extend(["--database-url", database_url])
    if postgres_url_env is not None:
        command.extend(["--postgres-url-env", postgres_url_env])
    return subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_memory_effectiveness_script_writes_passing_report(tmp_path: Path) -> None:
    output_path = tmp_path / "report.json"

    result = _run_script(output_path=output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert result.returncode == 0
    assert "status=passed" in result.stdout
    assert payload["passed_cases"] == 4
    assert output_path.read_text(encoding="utf-8").endswith("\n")


def test_memory_effectiveness_script_returns_nonzero_for_failed_case(tmp_path: Path) -> None:
    payload = json.loads(Path("evaluation/m28_memory_effectiveness_suite.json").read_text())
    payload["cases"][3]["expected_candidates"][0]["context_disposition"] = "suppressed"
    suite_path = tmp_path / "failing-suite.json"
    output_path = tmp_path / "report.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_script(output_path=output_path, suite_path=suite_path)

    assert result.returncode == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "failed"


def test_memory_effectiveness_script_accepts_disposable_database_url(tmp_path: Path) -> None:
    output_path = tmp_path / "report.json"
    database_path = tmp_path / "evaluation.db"

    result = _run_script(
        output_path=output_path,
        database_url=f"sqlite:///{database_path}",
    )

    assert result.returncode == 0
    assert database_path.exists()


def test_memory_effectiveness_script_database_url_overrides_ambient_database_url(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "report.json"
    target_database = tmp_path / "target.db"
    ambient_database = tmp_path / "ambient.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{ambient_database}"

    result = _run_script(
        output_path=output_path,
        database_url=f"sqlite:///{target_database}",
        env=env,
    )

    assert result.returncode == 0
    assert target_database.exists()
    assert not ambient_database.exists()


def test_memory_effectiveness_script_reads_database_url_from_environment(tmp_path: Path) -> None:
    output_path = tmp_path / "report.json"
    database_path = tmp_path / "evaluation-env.db"
    env = os.environ.copy()
    env["M28_EFFECTIVENESS_DATABASE_URL"] = f"sqlite:///{database_path}"

    result = _run_script(
        output_path=output_path,
        postgres_url_env="M28_EFFECTIVENESS_DATABASE_URL",
        env=env,
    )

    assert result.returncode == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "passed"


def test_memory_effectiveness_script_errors_for_missing_database_environment(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.pop("M28_EFFECTIVENESS_MISSING_URL", None)

    result = _run_script(
        output_path=tmp_path / "report.json",
        postgres_url_env="M28_EFFECTIVENESS_MISSING_URL",
        env=env,
    )

    assert result.returncode == 2
    assert "M28_EFFECTIVENESS_MISSING_URL" in result.stderr

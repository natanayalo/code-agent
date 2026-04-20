"""Integration-style checks for GitHub Actions CI workflow hardening."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    assert isinstance(data, dict)
    return data


def _workflow_triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    triggers = workflow.get("on", workflow.get(True))

    assert isinstance(triggers, dict)
    return triggers


def _job_steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    jobs = workflow["jobs"]
    job = jobs[job_name]
    steps = job["steps"]

    assert isinstance(steps, list)
    return steps


def _step_by_name(steps: list[dict[str, Any]], step_name: str) -> dict[str, Any]:
    for step in steps:
        if step.get("name") == step_name:
            return step

    msg = f"Expected CI step '{step_name}' to exist."
    raise AssertionError(msg)


def test_pyproject_dev_dependencies_include_pytest_cov() -> None:
    """Coverage gating in CI depends on pytest-cov being available in dev installs."""
    with Path("pyproject.toml").open("rb") as file:
        config = tomllib.load(file)

    dev_dependencies = config["tool"]["poetry"]["group"]["dev"]["dependencies"]
    assert "pytest-cov" in dev_dependencies
    pytest_cov = dev_dependencies["pytest-cov"]
    version_spec = pytest_cov if isinstance(pytest_cov, str) else pytest_cov.get("version", "")
    assert isinstance(version_spec, str) and version_spec.startswith(">=")


def test_pyproject_dev_dependencies_include_pytest_asyncio() -> None:
    """Async test support should be explicit in the dev dependency set."""
    with Path("pyproject.toml").open("rb") as file:
        config = tomllib.load(file)

    dev_dependencies = config["tool"]["poetry"]["group"]["dev"]["dependencies"]
    assert "pytest-asyncio" in dev_dependencies
    pytest_asyncio = dev_dependencies["pytest-asyncio"]
    version_spec = (
        pytest_asyncio if isinstance(pytest_asyncio, str) else pytest_asyncio.get("version", "")
    )
    assert isinstance(version_spec, str) and version_spec.startswith(">=")


def test_pytest_workflow_runs_on_push_and_enforces_coverage() -> None:
    """The pytest workflow should validate each push with a coverage gate."""
    workflow = _load_yaml(".github/workflows/pytest.yml")
    triggers = _workflow_triggers(workflow)
    steps = _job_steps(workflow, "pytest")
    plugin_step = _step_by_name(steps, "Verify async pytest plugin availability")
    run_step = _step_by_name(steps, "Run pytest with coverage gate")
    upload_step = _step_by_name(steps, "Upload coverage artifact")

    assert "push" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert workflow["jobs"]["pytest"]["timeout-minutes"] == 15
    assert "import pytest_asyncio" in plugin_step["run"]
    assert run_step["run"]
    for expected_flag in (
        "--cov=apps",
        "--cov=db",
        "--cov=memory",
        "--cov=orchestrator",
        "--cov=repositories",
        "--cov=sandbox",
        "--cov=tools",
        "--cov=workers",
        "--cov-branch",
        "--cov-report=xml",
        "--cov-fail-under=90",
    ):
        assert expected_flag in run_step["run"]

    assert upload_step["uses"] == "actions/upload-artifact@v7"
    assert upload_step["with"]["path"] == "coverage.xml"


def test_pre_commit_workflow_runs_on_push_without_ci_branch_guard_failures() -> None:
    """CI should lint each push while skipping the local branch-guard hook in Actions."""
    workflow = _load_yaml(".github/workflows/pre-commit.yml")
    triggers = _workflow_triggers(workflow)
    steps = _job_steps(workflow, "pre-commit")
    run_step = _step_by_name(steps, "Run pre-commit")

    assert "push" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert workflow["jobs"]["pre-commit"]["timeout-minutes"] == 10
    assert workflow["jobs"]["pre-commit"]["env"]["SKIP"] == "no-commit-to-branch"
    assert "--hook-stage manual" in run_step["run"]


def test_frozen_eval_workflow_runs_harness_and_uploads_report() -> None:
    """Frozen evaluation should run on push and upload a deterministic JSON report."""
    workflow = _load_yaml(".github/workflows/frozen-eval.yml")
    triggers = _workflow_triggers(workflow)
    steps = _job_steps(workflow, "frozen-eval")
    run_step = _step_by_name(steps, "Run frozen evaluation harness")
    upload_step = _step_by_name(steps, "Upload frozen evaluation report")

    assert "push" in triggers
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert workflow["jobs"]["frozen-eval"]["timeout-minutes"] == 10
    assert "python scripts/e2e/run_frozen_eval.py" in run_step["run"]
    assert "--runner orchestrator" in run_step["run"]
    assert "artifacts/evaluations/frozen-suite-report.json" in run_step["run"]
    assert upload_step["uses"] == "actions/upload-artifact@v7"
    assert upload_step["with"]["path"] == "artifacts/evaluations/frozen-suite-report.json"


def test_pip_audit_workflow_declares_read_only_token_permissions() -> None:
    """pip-audit workflow should explicitly scope GITHUB_TOKEN permissions."""
    workflow = _load_yaml(".github/workflows/pip-audit.yml")
    steps = _job_steps(workflow, "pip-audit")
    run_step = _step_by_name(steps, "Audit Python dependencies")

    assert workflow["permissions"] == {"contents": "read"}
    assert "pip-audit" in run_step["run"]


def test_pre_commit_config_keeps_local_default_branch_guard() -> None:
    """Developers should still be blocked from committing directly to main/master locally."""
    config = _load_yaml(".pre-commit-config.yaml")

    all_hooks = (hook for repo in config["repos"] for hook in repo["hooks"])
    branch_guard_hook = next(
        (hook for hook in all_hooks if hook["id"] == "no-commit-to-branch"),
        None,
    )

    assert branch_guard_hook is not None
    assert branch_guard_hook["args"] == ["--branch", "main", "--branch", "master"]
    assert branch_guard_hook["stages"] == ["pre-commit"]

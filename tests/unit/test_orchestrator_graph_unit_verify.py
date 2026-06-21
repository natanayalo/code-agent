# ruff: noqa: F403, F405
"""Verification-focused orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_verify_result_passed():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    res = verify_result(state)
    assert res["current_step"] == "verify_result"
    assert res["verification"]["status"] == "passed"
    assert len(res["verification"]["items"]) == 5


def test_verify_result_failed_tests():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "test_regression"
    assert res["verification"]["items"][1]["label"] == "tests"
    assert res["verification"]["items"][1]["status"] == "failed"


def test_verify_result_warning_no_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": [],
                "test_results": [{"name": "test1", "status": "passed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "warning"
    assert res["verification"]["items"][2]["label"] == "file_changes"
    assert res["verification"]["items"][2]["status"] == "warning"


def test_verify_result_failed_with_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "files_changed": ["partial.py"],
                "test_results": [],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "failed"
    file_changes = next(i for i in res["verification"]["items"] if i["label"] == "file_changes")
    assert file_changes["status"] == "warning"
    assert "but changed 1 files" in file_changes["message"]


def test_verify_result_queues_repair_for_repairable_worker_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo", "budget": {"max_retries": 1}},
            "result": {
                "status": "failure",
                "failure_kind": "compile",
                "summary": "Compile step failed.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [],
                "commands_run": [{"command": "pytest", "exit_code": 1}],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "worker_failure"
    assert res["repair_handoff_requested"] is True
    assert "queued bounded repair handoff (1/1)" in res["progress_updates"][-1]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1


def test_verify_result_skips_repair_for_non_repairable_worker_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo", "budget": {"max_retries": 1}},
            "result": {
                "status": "failure",
                "failure_kind": "provider_error",
                "summary": "Provider unavailable.",
                "files_changed": [],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "worker_failure"
    assert "repair_handoff_requested" not in res
    assert "task" not in res
    assert res["progress_updates"][-1] == "verification failed"


def test_verify_result_surfaces_post_run_lint_warnings() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["workers/codex_cli_worker.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "budget_usage": {
                    "post_run_lint_format": {
                        "ran": True,
                        "errors": [
                            "`ruff check --fix -- workers/codex_cli_worker.py` exited with status 1"
                        ],
                    }
                },
            },
        }
    )

    res = verify_result(state)

    lint_check = next(
        item for item in res["verification"]["items"] if item["label"] == "post_run_lint_format"
    )
    assert lint_check["status"] == "warning"
    assert "reported 1 issue" in lint_check["message"]
    assert res["verification"]["status"] == "warning"


def test_verify_result_marks_post_run_lint_skip_as_passed() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "budget_usage": {"post_run_lint_format": {"ran": False, "status": "skipped"}},
            },
        }
    )

    res = verify_result(state)

    lint_check = next(
        item for item in res["verification"]["items"] if item["label"] == "post_run_lint_format"
    )
    assert lint_check["status"] == "passed"
    assert "skipped" in lint_check["message"]


def test_verify_result_queues_bounded_repair_handoff_after_verifier_failure() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "budget": {"max_retries": 1},
            },
            "task_spec": {"goal": "demo", "verification_commands": ["pytest -q tests/unit"]},
            "result": {
                "status": "success",
                "summary": "Applied change set.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["repair_handoff_requested"] is True
    assert "queued bounded repair handoff (1/1)" in res["progress_updates"][-1]
    constraints = res["task"]["constraints"]
    assert constraints["independent_verifier_repair_passes_used"] == 1
    repair_text = constraints["independent_verifier_repair_request"]
    assert "Apply targeted code fixes for failed verification checks." in repair_text
    assert "pytest -q tests/unit" in repair_text
    assert (
        "Light investigation is allowed only when directly tied to explaining "
        "a failed verification check." in repair_text
    )
    assert "Do not perform broad repo debugging" in repair_text


def test_verify_result_verifier_repair_budget_is_decoupled_from_max_retries() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "budget": {"max_retries": 0, "max_verifier_passes": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied change set.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert res["repair_handoff_requested"] is True
    constraints = res["task"]["constraints"]
    assert constraints["independent_verifier_repair_passes_used"] == 1


def test_verify_result_stops_after_bounded_repair_attempts() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_verifier_repair_request": "repair text",
                    "independent_verifier_repair_passes_used": 1,
                },
                "budget": {"max_retries": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied repair candidate.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "failed"
    assert "repair_handoff_requested" not in res
    assert "independent_verifier_repair_request" not in res["task"]["constraints"]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1
    assert "verification failed after bounded repair attempts" in res["progress_updates"][-1]
    assert (
        "Verification is still failing after 1 bounded repair attempt" in res["result"]["summary"]
    )
    assert res["result"]["next_action_hint"] == "await_manual_follow_up"


def test_verify_result_cleans_verifier_repair_task_after_successful_follow_up() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "demo",
                "constraints": {
                    "independent_verifier_repair_request": "repair text",
                    "independent_verifier_repair_passes_used": 1,
                },
                "budget": {"max_retries": 1},
            },
            "result": {
                "status": "success",
                "summary": "Applied repair candidate.",
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
            },
        }
    )

    res = verify_result(state)

    assert res["verification"]["status"] == "passed"
    assert "repair_handoff_requested" not in res
    assert "result" not in res
    assert "independent_verifier_repair_request" not in res["task"]["constraints"]
    assert res["task"]["constraints"]["independent_verifier_repair_passes_used"] == 1
    assert "verification passed after bounded repair handoff" in res["progress_updates"][-1]


def test_verify_result_runs_independent_verifier_when_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {
                "goal": "demo",
                "verification_commands": ["ls"],
                "allowed_actions": ["modify_workspace_files"],
            },
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("passed", "independent verification passed"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "passed"
    assert res["verification"]["status"] == "passed"


def test_verify_result_fails_when_independent_verifier_command_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {
                "goal": "demo",
                "verification_commands": ["ls does-not-exist"],
                "allowed_actions": ["modify_workspace_files"],
            },
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("failed", "independent verification failed"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "failed"
    assert res["verification"]["status"] == "failed"
    assert res["verification"]["failure_kind"] == "test_regression"


def test_verify_result_warns_when_independent_verifier_command_is_unsafe(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "task_spec": {
                "goal": "demo",
                "verification_commands": ["rm -rf ."],
                "allowed_actions": ["modify_workspace_files"],
            },
            "result": {
                "status": "success",
                "files_changed": ["README.md"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [],
                "artifacts": [
                    {
                        "name": "workspace",
                        "uri": workspace.as_uri(),
                        "artifact_type": "workspace",
                    }
                ],
            },
        }
    )

    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("warning", "independent verifier warned"),
    )

    independent_check = next(
        item for item in res["verification"]["items"] if item["label"] == "independent_verifier"
    )
    assert independent_check["status"] == "warning"
    assert res["verification"]["status"] == "warning"


def test_verify_result_with_independent_verifier() -> None:
    """Verify handling of independent verifier outcome."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "do something"},
            "result": {
                "status": "success",
                "summary": "ok",
                "commands_run": [],
                "files_changed": [],
                "artifacts": [],
            },
        }
    )
    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("failed", "Verifier found issue"),
    )
    items = res["verification"]["items"]
    iv_item = next(i for i in items if i["label"] == "independent_verifier")
    assert iv_item["status"] == "failed"
    assert iv_item["message"] == "Verifier found issue"


def test_verify_result_failure_kinds() -> None:
    """Verify that different failure labels map to correct failure kinds."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "do"},
            "result": {
                "status": "failure",
                "summary": "failed",
                "commands_run": [],
                "files_changed": [],
                "artifacts": [],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["failure_kind"] == "worker_failure"

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "do"},
            "result": {
                "status": "success",
                "summary": "ok",
                "commands_run": [],
                "files_changed": ["ok.txt"],
                "artifacts": [],
            },
        }
    )
    res = verify_result(state, deterministic_verifier_outcome=("failed", "Command failed"))
    assert res["verification"]["failure_kind"] == "test_regression"

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "do"},
            "result": {
                "status": "success",
                "summary": "ok",
                "commands_run": [],
                "files_changed": [],
                "artifacts": [],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "warning"
    assert res["verification"]["failure_kind"] is None


def test_verify_result_allows_no_files_for_review_tasks() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Review orchestrator quality and compare patterns"},
            "result": {
                "status": "success",
                "summary": "Completed.",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "warning"
    assert res["verification"]["failure_kind"] is None
    file_changes = next(i for i in res["verification"]["items"] if i["label"] == "file_changes")
    assert file_changes["status"] == "warning"
    assert res.get("result") is None or res["result"]["status"] == "success"


def test_verify_result_attaches_independent_verifier_reason_code() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Implement change"},
            "result": {
                "status": "success",
                "summary": "Completed.",
                "commands_run": [],
                "files_changed": ["orchestrator/graph.py"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    res = verify_result(
        state,
        enable_independent_verifier=True,
        independent_verifier_outcome=("warning", "Verifier infra unavailable"),
        independent_verifier_reason_code="infra_verifier_unavailable",
    )
    item = next(i for i in res["verification"]["items"] if i["label"] == "independent_verifier")
    assert item["reason_code"] == "infra_verifier_unavailable"

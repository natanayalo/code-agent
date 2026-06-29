from typing import Any, cast

from orchestrator.nodes.verification_result import _check_file_changes
from orchestrator.state import OrchestratorState


def test_protected_paths_unapproved():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "t1",
                "task_text": "do thing",
                "repo_url": "foo",
            },
            "repo_profile": {
                "protected_paths": ["db/migrations/*", "config/*.yaml"],
            },
            "result": {
                "status": "success",
                "summary": "did it",
                "files_changed": ["db/migrations/123.py", "app.py"],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    report_item = _check_file_changes(state)
    assert report_item.status == "failed"
    assert report_item.reason_code == "unapproved_protected_path"
    assert "db/migrations/123.py matched db/migrations/*" in report_item.message


def test_protected_paths_unapproved_bare_directory_pattern():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "t1",
                "task_text": "do thing",
                "repo_url": "foo",
            },
            "repo_profile": {
                "protected_paths": ["db/migrations"],
            },
            "result": {
                "status": "success",
                "summary": "did it",
                "files_changed": ["db/migrations/123.py"],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    report_item = _check_file_changes(state)
    assert report_item.status == "failed"
    assert report_item.reason_code == "unapproved_protected_path"
    assert "db/migrations/123.py matched db/migrations" in report_item.message


def test_protected_paths_are_case_sensitive():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "t1",
                "task_text": "do thing",
                "repo_url": "foo",
            },
            "repo_profile": {
                "protected_paths": ["DB/Migrations"],
            },
            "result": {
                "status": "success",
                "summary": "did it",
                "files_changed": ["db/migrations/123.py"],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    report_item = _check_file_changes(state)
    assert report_item.status == "passed"


def test_protected_paths_normalize_nullable_and_blank_patterns():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "t1",
                "task_text": "do thing",
                "repo_url": "foo",
            },
            "repo_profile": {
                "protected_paths": [],
            },
            "result": {
                "status": "success",
                "summary": "did it",
                "files_changed": ["db/migrations/123.py"],
                "test_results": [],
                "commands_run": [],
            },
        }
    )
    assert state.repo_profile is not None
    cast(Any, state.repo_profile).protected_paths = [None, "   ", "  db/migrations  "]

    report_item = _check_file_changes(state)
    assert report_item.status == "failed"
    assert report_item.reason_code == "unapproved_protected_path"
    assert "db/migrations/123.py matched db/migrations" in report_item.message


def test_protected_paths_approved():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "t1",
                "task_text": "do thing",
                "repo_url": "foo",
            },
            "repo_profile": {
                "protected_paths": ["db/migrations/*", "config/*.yaml"],
            },
            "approval": {
                "status": "approved",
                "reason": "approved by admin",
            },
            "result": {
                "status": "success",
                "summary": "did it",
                "files_changed": ["db/migrations/123.py", "app.py"],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    report_item = _check_file_changes(state)
    assert report_item.status == "passed"
    assert report_item.label == "file_changes"

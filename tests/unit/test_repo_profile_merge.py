from typing import Any, cast

from orchestrator.repo_profile import RepoProfile
from orchestrator.state import TaskSpec
from orchestrator.task_spec import apply_repo_profile_to_task_spec


def test_apply_repo_profile_to_task_spec_overrides():
    task_spec = TaskSpec(
        goal="Do a thing",
        verification_commands=[],
        setup_commands=["make setup"],
        risk_level="medium",
    )

    profile = RepoProfile.model_validate(
        {
            "setup": {"commands": ["npm ci"]},
            "validation": {"quick": ["npm run test:smoke"], "full": ["npm test", "npm run lint"]},
        }
    )

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    # Setup commands should be overridden
    assert merged.setup_commands == ["npm ci"]

    # For medium risk, full validation should override verification commands
    assert merged.verification_commands == ["npm test", "npm run lint"]


def test_apply_repo_profile_no_override_if_no_commands():
    task_spec = TaskSpec(
        goal="Do a thing",
        verification_commands=["pytest specific_test.py"],
        setup_commands=["make setup"],
        risk_level="medium",
    )

    # empty profile
    profile = RepoProfile()

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.setup_commands == ["make setup"]
    assert merged.verification_commands == ["pytest specific_test.py"]


def test_apply_repo_profile_default_smoke_override():
    # Setup default smoke command that is typically overridden
    task_spec = TaskSpec(
        goal="Do a thing",
        verification_commands=["printf '%s' $PWD"],
        setup_commands=[],
        risk_level="low",
    )

    profile = RepoProfile.model_validate({"validation": {"quick": ["make test-fast"]}})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    # For low risk, quick validation should override the default placeholder
    assert merged.verification_commands == ["make test-fast"]


def test_apply_repo_profile_respects_custom_verification():
    task_spec = TaskSpec(
        goal="Do a thing",
        verification_commands=["pytest specific_test.py"],
        setup_commands=[],
        risk_level="high",
    )

    profile = RepoProfile.model_validate({"validation": {"full": ["make test-all"]}})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    # Custom verification commands should NOT be overridden
    assert merged.verification_commands == ["pytest specific_test.py"]


def test_apply_repo_profile_escalates_character_set_protected_path_globs():
    task_spec = TaskSpec(
        goal="Update config/app1.yaml",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"protected_paths": ["config/app[0-9].yaml"]})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "high"
    assert merged.requires_permission is True
    assert merged.permission_reason == "Task may affect protected paths"


def test_apply_repo_profile_escalates_directory_glob_with_non_word_tail():
    task_spec = TaskSpec(
        goal="Clean up db/migrations/",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"protected_paths": ["db/migrations/*"]})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "high"
    assert merged.requires_permission is True
    assert merged.permission_reason == "Task may affect protected paths"


def test_apply_repo_profile_ignores_nullable_protected_path_entries():
    task_spec = TaskSpec(
        goal="Update db/migrations/001_init.py",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"protected_paths": ["db/migrations"]})
    cast(Any, profile).protected_paths = [None, "   ", "db/migrations"]

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "high"
    assert merged.requires_permission is True
    assert merged.permission_reason == "Task may affect protected paths"


def test_apply_repo_profile_protected_path_escalation_is_case_sensitive():
    task_spec = TaskSpec(
        goal="Update DB/Migrations/001_init.py",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"protected_paths": ["db/migrations"]})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "low"
    assert merged.requires_permission is False
    assert merged.permission_reason is None


def test_apply_repo_profile_approval_required_uses_whole_word_matching():
    task_spec = TaskSpec(
        goal="Investigate undeployed environment state",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"approval_required": ["deploy"]})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "low"
    assert merged.requires_permission is False
    assert merged.permission_reason is None


def test_apply_repo_profile_approval_required_escalates_whole_word_match():
    task_spec = TaskSpec(
        goal="deploy the service",
        verification_commands=[],
        setup_commands=[],
        risk_level="low",
    )
    profile = RepoProfile.model_validate({"approval_required": ["deploy"]})

    merged = apply_repo_profile_to_task_spec(task_spec, profile)

    assert merged.risk_level == "high"
    assert merged.requires_permission is True
    assert merged.permission_reason == "Task may involve approval-required category: deploy"

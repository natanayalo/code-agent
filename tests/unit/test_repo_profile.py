from orchestrator.repo_profile import RepoProfile


def test_repo_profile_parsing():
    profile = RepoProfile.model_validate(
        {
            "setup": {"commands": ["npm ci", "npm run build"]},
            "validation": {
                "quick": ["npm run test:smoke"],
                "full": ["npm test", "npm run lint"],
            },
            "delivery": {"default_mode": "branch"},
            "protected_paths": ["src/core/"],
        }
    )

    assert profile.setup.commands == ["npm ci", "npm run build"]
    assert profile.validation.quick == ["npm run test:smoke"]
    assert profile.validation.full == ["npm test", "npm run lint"]
    assert profile.delivery.default_mode == "branch"
    assert profile.protected_paths == ["src/core/"]


def test_repo_profile_defaults():
    profile = RepoProfile.model_validate({})

    assert profile.setup.commands == []
    assert profile.validation.quick == []
    assert profile.validation.full == []
    assert profile.delivery.default_mode == "workspace"
    assert profile.protected_paths == []

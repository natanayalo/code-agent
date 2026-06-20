from __future__ import annotations

import pytest

from apps.api.config import SystemConfig
from sandbox.container import DEFAULT_SANDBOX_IMAGE
from sandbox.workspace import default_workspace_root


def test_load_from_env_uses_provided_mapping_for_workspace_root_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provided env mappings should not leak host workspace-root overrides."""
    monkeypatch.setenv("CODE_AGENT_WORKSPACE_ROOT", "/tmp/host-workspace-root")

    config = SystemConfig.load_from_env({"CODE_AGENT_SANDBOX_IMAGE": "custom-image"})

    assert config.default_image == "custom-image"
    assert config.workspace_root == str(default_workspace_root({}))
    assert config.workspace_root != "/tmp/host-workspace-root"


def test_load_from_env_normalizes_explicit_values() -> None:
    """Whitespace should be stripped before using explicit configuration values."""
    config = SystemConfig.load_from_env(
        {
            "CODE_AGENT_SANDBOX_IMAGE": "  custom-sandbox-image  ",
            "CODE_AGENT_WORKSPACE_ROOT": "  /tmp/custom-workspace-root  ",
        }
    )

    assert config.default_image == "custom-sandbox-image"
    assert config.workspace_root == "/tmp/custom-workspace-root"


def test_load_from_env_falls_back_to_default_image_when_blank() -> None:
    """Blank image configuration should fall back to the default sandbox image."""
    config = SystemConfig.load_from_env({"CODE_AGENT_SANDBOX_IMAGE": "   "})

    assert config.default_image == DEFAULT_SANDBOX_IMAGE


def test_load_from_env_parses_non_negative_scout_triggers() -> None:
    """Scout trigger intervals should support zero to disable them."""
    config = SystemConfig.load_from_env(
        {
            "CODE_AGENT_SCOUT_IDLE_MINUTES": "0",
            "CODE_AGENT_SCOUT_SCHEDULE_INTERVAL_MINUTES": "0",
        }
    )

    assert config.scout_idle_trigger_minutes == 0
    assert config.scout_schedule_interval_minutes == 0


def test_load_from_env_parses_scout_allowed_repos() -> None:
    """Allowed repos should be parsed correctly, handling whitespace and ignoring
    invalid entries.
    """
    config = SystemConfig.load_from_env(
        {
            "CODE_AGENT_SCOUT_ALLOWED_REPOS": (
                "repo1:https://github.com/repo1, repo2 : https://github.com/repo2 , "
                "invalid, :nou , keyonly:  "
            )
        }
    )

    assert config.scout_allowed_repos == {
        "repo1": "https://github.com/repo1",
        "repo2": "https://github.com/repo2",
    }

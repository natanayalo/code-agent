from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox.policy import (
    LocalRepoPolicyError,
    PathPolicy,
    is_allowed_local_remote,
    is_in_container,
    raise_if_sibling_workspace,
    validate_local_repo_path,
)


def test_is_in_container_dockerenv():
    with patch("os.path.exists") as mock_exists:
        mock_exists.side_effect = lambda p: p == "/.dockerenv"
        assert is_in_container() is True


def test_is_in_container_cgroup_docker():
    with (
        patch("os.path.exists") as mock_exists,
        patch("pathlib.Path.exists") as mock_path_exists,
        patch("pathlib.Path.read_text") as mock_read_text,
    ):
        mock_exists.return_value = False
        mock_path_exists.side_effect = lambda: True
        mock_read_text.return_value = "1:name=systemd:/docker/123456"

        assert is_in_container() is True


def test_is_in_container_cgroup_containerd():
    with (
        patch("os.path.exists") as mock_exists,
        patch("pathlib.Path.exists") as mock_path_exists,
        patch("pathlib.Path.read_text") as mock_read_text,
    ):
        mock_exists.return_value = False
        mock_path_exists.side_effect = lambda: True
        mock_read_text.return_value = "1:name=systemd:/containerd/123456"

        assert is_in_container() is True


def test_is_in_container_cgroup_kubepods():
    with (
        patch("os.path.exists") as mock_exists,
        patch("pathlib.Path.exists") as mock_path_exists,
        patch("pathlib.Path.read_text") as mock_read_text,
    ):
        mock_exists.return_value = False
        mock_path_exists.side_effect = lambda: True
        mock_read_text.return_value = "1:name=systemd:/kubepods/besteffort/pod123"

        assert is_in_container() is True


def test_is_not_in_container():
    with patch("os.path.exists") as mock_exists, patch("pathlib.Path.exists") as mock_path_exists:
        mock_exists.return_value = False
        mock_path_exists.return_value = False

        assert is_in_container() is False


def test_is_in_container_cgroup_permission_error():
    with (
        patch("os.path.exists") as mock_exists,
        patch("pathlib.Path.exists") as mock_path_exists,
        patch("pathlib.Path.read_text") as mock_read_text,
    ):
        mock_exists.return_value = False
        mock_path_exists.return_value = True
        mock_read_text.side_effect = OSError("Permission denied")

        assert is_in_container() is False


def test_path_policy_enforcement():
    policy = PathPolicy(
        allowed_prefixes=["/workspace", "/tmp/shared"],
        denied_prefixes=["/workspace/.git", "/workspace/secrets.txt"],
    )

    # Allowed
    assert policy.check_path("/workspace/src/app.py") is True
    assert policy.check_path("/tmp/shared/data.json") is True
    assert policy.check_path(Path("/workspace/README.md")) is True

    # Denied by prefix
    assert policy.check_path("/workspace/.git/config") is False
    assert policy.check_path("/workspace/secrets.txt") is False

    # Denied (outside prefixes)
    assert policy.check_path("/etc/passwd") is False
    assert policy.check_path("/home/user/.bashrc") is False

    # Normalization
    assert policy.check_path("/workspace/../etc/passwd") is False
    assert policy.check_path("/workspace/./src/app.py") is True


def test_path_policy_value_error_handling():
    policy = PathPolicy(allowed_prefixes=["/workspace"])
    # Trigger ValueError in is_relative_to by mocking it
    with patch("pathlib.Path.is_relative_to", side_effect=ValueError("Test")):
        assert policy.check_path("/workspace/test.py") is False


def _raise_cross_drive(*args: object, **kwargs: object) -> bool:
    raise ValueError("different drives")


def test_is_allowed_local_remote_ignores_cross_drive_relative_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODE_AGENT_ALLOWED_LOCAL_REMOTES", str(tmp_path))
    monkeypatch.setattr(Path, "is_relative_to", _raise_cross_drive)

    assert is_allowed_local_remote(tmp_path / "repo") is False


def test_raise_if_sibling_workspace_ignores_cross_drive_relative_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "is_relative_to", _raise_cross_drive)
    monkeypatch.setattr(Path, "relative_to", _raise_cross_drive)

    raise_if_sibling_workspace(
        resolved_path=tmp_path / "repo",
        workspace_path=tmp_path / "workspace-current",
        allowed_root=tmp_path / "workspaces",
    )


def test_validate_local_repo_path_reports_policy_error_for_cross_drive_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "is_relative_to", _raise_cross_drive)

    with pytest.raises(LocalRepoPolicyError, match="outside the allowed workspace root"):
        validate_local_repo_path(str(tmp_path / "repo"))

from pathlib import Path
from unittest.mock import patch

from sandbox.policy import PathPolicy, is_in_container


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

from unittest.mock import patch

from sandbox.policy import is_in_container


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

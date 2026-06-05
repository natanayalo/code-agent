import pytest


@pytest.fixture(autouse=True)
def _mock_home_directory(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Ensure tests never read from or write to the real home directory.
    This prevents SDKs (like Google GenAI) from failing in CI when HOME is read-only
    or polluting the local developer environment.
    """
    home_dir = tmp_path_factory.mktemp("mock_home")
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))

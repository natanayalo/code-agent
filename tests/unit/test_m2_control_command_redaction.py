"""Credential-bearing Docker argv must not escape through launch errors."""

import subprocess
import traceback
from unittest.mock import patch

import pytest

from sandbox.container import DockerSandboxContainerError, _run_docker_command


@pytest.mark.parametrize("kind", ["os", "timeout"])
def test_control_command_error_does_not_disclose_environment_or_cause(kind):
    secret = "synthetic-credential-canary"
    command = ["docker", "run", "--env", f"PROVIDER_TOKEN={secret}", "synthetic-image"]
    error = OSError(secret) if kind == "os" else subprocess.TimeoutExpired(command, timeout=3)
    with patch("sandbox.container.subprocess.run", side_effect=error):
        with pytest.raises(DockerSandboxContainerError) as captured:
            _run_docker_command(command, timeout=3)
    rendered = "".join(traceback.format_exception(captured.value))
    assert secret not in rendered and "PROVIDER_TOKEN" not in rendered
    assert captured.value.__suppress_context__

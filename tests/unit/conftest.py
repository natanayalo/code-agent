"""Unit-only native runner double.

Production native runs have no host subprocess fallback.  Existing provider
parser tests use small local scripts, so this double keeps those tests focused
on result mapping while executor hardening is covered separately.
"""

from __future__ import annotations

import pytest

import workers.native_agent_runner as native_runner
from tests.native_agent_test_doubles import LocalNativeAgentRunner


@pytest.fixture(autouse=True)
def _use_local_native_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_runner, "DockerNativeAgentExecutor", LocalNativeAgentRunner)

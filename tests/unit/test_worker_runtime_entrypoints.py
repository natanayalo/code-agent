"""Unit tests for worker runtime entrypoints and runtime mode helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps import runtime as runtime_module
from apps.worker import main as worker_main


class _FakeAsyncClient:
    def __init__(self, label: str, calls: list[str]) -> None:
        self.label = label
        self.calls = calls

    async def aclose(self) -> None:
        self.calls.append(self.label)


def test_runtime_is_enabled_truthy_and_falsy_values() -> None:
    """Runtime mode helper should parse common truthy and falsy env values."""
    assert runtime_module._is_enabled("1", default=False)
    assert runtime_module._is_enabled(" TRUE ", default=False)
    assert runtime_module._is_enabled("yes", default=False)
    assert not runtime_module._is_enabled("0", default=True)
    assert not runtime_module._is_enabled("off", default=True)
    assert runtime_module._is_enabled(None, default=True)
    assert not runtime_module._is_enabled(None, default=False)


def test_runtime_mode_defaults_and_overrides() -> None:
    """API defaults on; worker defaults off unless env enables it."""
    assert runtime_module.should_run_api({}) is True
    assert runtime_module.should_run_worker({}) is False
    assert runtime_module.should_run_api({runtime_module.RUN_API_ENV_VAR: "false"}) is False
    assert runtime_module.should_run_worker({runtime_module.RUN_WORKER_ENV_VAR: "on"}) is True


def test_runtime_coerce_positive_int_env_defaults_and_overrides() -> None:
    """Shared env int parser should return defaults on invalid values."""
    assert runtime_module.coerce_positive_int_env(None, default=5) == 5
    assert runtime_module.coerce_positive_int_env("", default=5) == 5
    assert runtime_module.coerce_positive_int_env("abc", default=5) == 5
    assert runtime_module.coerce_positive_int_env("0", default=5) == 5
    assert runtime_module.coerce_positive_int_env("-1", default=5) == 5
    assert runtime_module.coerce_positive_int_env("9", default=5) == 9


def test_worker_coerce_positive_float_defaults_on_invalid_values() -> None:
    """Float parser should keep defaults for missing/invalid/non-positive values."""
    assert worker_main._coerce_positive_float(None, default=2.0) == 2.0
    assert worker_main._coerce_positive_float("", default=2.0) == 2.0
    assert worker_main._coerce_positive_float("abc", default=2.0) == 2.0
    assert worker_main._coerce_positive_float("0", default=2.0) == 2.0
    assert worker_main._coerce_positive_float("-1", default=2.0) == 2.0
    assert worker_main._coerce_positive_float("3.5", default=2.0) == 3.5


def test_worker_coerce_positive_int_defaults_on_invalid_values() -> None:
    """Int parser should keep defaults for missing/invalid/non-positive values."""
    assert worker_main._coerce_positive_int(None, default=60) == 60
    assert worker_main._coerce_positive_int("", default=60) == 60
    assert worker_main._coerce_positive_int("abc", default=60) == 60
    assert worker_main._coerce_positive_int("0", default=60) == 60
    assert worker_main._coerce_positive_int("-2", default=60) == 60
    assert worker_main._coerce_positive_int("90", default=60) == 90


@pytest.mark.anyio
async def test_run_worker_forever_requires_enabled_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker entrypoint should fail fast when runtime mode disables worker process."""
    monkeypatch.setattr(worker_main, "should_run_worker", lambda: False)

    with pytest.raises(RuntimeError, match="Worker runtime is disabled"):
        await worker_main.run_worker_forever()


@pytest.mark.anyio
async def test_run_worker_forever_requires_bootstrapped_service_and_closes_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing task service config should raise and still close outbound clients."""
    close_calls: list[str] = []
    outbound_clients = SimpleNamespace(
        telegram=_FakeAsyncClient("telegram", close_calls),
        webhook=_FakeAsyncClient("webhook", close_calls),
    )

    monkeypatch.setattr(worker_main, "should_run_worker", lambda: True)
    monkeypatch.setattr(
        worker_main,
        "create_outbound_http_clients",
        lambda: outbound_clients,
    )
    monkeypatch.setattr(
        worker_main,
        "build_task_service_from_env",
        lambda **_: None,
    )

    with pytest.raises(RuntimeError, match="requires CODE_AGENT_ENABLE_TASK_SERVICE=1"):
        await worker_main.run_worker_forever()

    assert set(close_calls) == {"telegram", "webhook"}


@pytest.mark.anyio
async def test_run_worker_forever_builds_queue_worker_from_env_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker runtime should build queue worker with env overrides and run it."""
    close_calls: list[str] = []
    outbound_clients = SimpleNamespace(
        telegram=_FakeAsyncClient("telegram", close_calls),
        webhook=_FakeAsyncClient("webhook", close_calls),
    )
    fake_service = object()
    worker_calls: list[dict[str, object]] = []

    class _FakeQueueWorker:
        def __init__(
            self,
            *,
            service: object,
            worker_id: str,
            poll_interval_seconds: float,
            lease_seconds: int,
        ) -> None:
            worker_calls.append(
                {
                    "service": service,
                    "worker_id": worker_id,
                    "poll_interval_seconds": poll_interval_seconds,
                    "lease_seconds": lease_seconds,
                }
            )

        async def run_forever(self) -> None:
            worker_calls.append({"ran": True})

    monkeypatch.setattr(worker_main, "should_run_worker", lambda: True)
    monkeypatch.setattr(
        worker_main,
        "create_outbound_http_clients",
        lambda: outbound_clients,
    )
    monkeypatch.setattr(
        worker_main,
        "build_task_service_from_env",
        lambda **_: fake_service,
    )
    monkeypatch.setattr(worker_main, "TaskQueueWorker", _FakeQueueWorker)
    monkeypatch.setenv(worker_main.WORKER_ID_ENV_VAR, "worker-test")
    monkeypatch.setenv(worker_main.POLL_INTERVAL_ENV_VAR, "4.5")
    monkeypatch.setenv(worker_main.LEASE_SECONDS_ENV_VAR, "75")

    await worker_main.run_worker_forever()

    assert worker_calls[0] == {
        "service": fake_service,
        "worker_id": "worker-test",
        "poll_interval_seconds": 4.5,
        "lease_seconds": 75,
    }
    assert worker_calls[1] == {"ran": True}
    assert set(close_calls) == {"telegram", "webhook"}


def test_worker_main_calls_async_entrypoint_with_configured_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() should configure logging and delegate to asyncio.run."""
    logging_calls: list[dict[str, object]] = []
    run_calls: list[object] = []
    otel_bootstrap_calls: list[dict[str, object]] = []

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(
        worker_main.logging,
        "basicConfig",
        lambda **kwargs: logging_calls.append(kwargs),
    )
    monkeypatch.setattr(
        worker_main.asyncio,
        "run",
        lambda coro: run_calls.append(coro),
    )
    monkeypatch.setattr(
        worker_main,
        "bootstrap_langsmith_otel",
        lambda **kwargs: otel_bootstrap_calls.append(kwargs),
    )

    worker_main.main()

    assert logging_calls == [{"level": "DEBUG"}]
    assert otel_bootstrap_calls == [{"runtime_name": "worker", "logger": worker_main.logger}]
    assert len(run_calls) == 1
    run_calls[0].close()

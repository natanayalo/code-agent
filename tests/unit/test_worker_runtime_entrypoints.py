"""Unit tests for worker runtime entrypoints and runtime mode helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps import runtime as runtime_module
from apps.worker import main as worker_main
from orchestrator.temporal import worker as temporal_worker


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


def test_temporal_only_cutover_at_requires_an_aware_iso_timestamp() -> None:
    """Drain metrics must not invent a boundary from invalid deployment config."""
    assert runtime_module.temporal_only_cutover_at({}) is None
    assert runtime_module.temporal_only_cutover_at({"TEMPORAL_ONLY_CUTOVER_AT": "invalid"}) is None
    assert (
        runtime_module.temporal_only_cutover_at({"TEMPORAL_ONLY_CUTOVER_AT": "2026-07-18T12:00:00"})
        is None
    )
    assert (
        runtime_module.temporal_only_cutover_at(
            {"TEMPORAL_ONLY_CUTOVER_AT": "2026-07-18T12:00:00Z"}
        ).isoformat()
        == "2026-07-18T12:00:00+00:00"
    )


@pytest.mark.anyio
async def test_run_worker_forever_requires_enabled_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker entrypoint should fail fast when runtime mode disables worker process."""
    monkeypatch.setattr(worker_main, "should_run_worker", lambda: False)

    with pytest.raises(RuntimeError, match="Worker runtime is disabled"):
        await worker_main.run_worker_forever()


@pytest.mark.anyio
async def test_temporal_worker_fails_after_bounded_connection_retries(monkeypatch) -> None:
    """A Temporal outage must terminate the worker instead of entering legacy polling."""
    attempts: list[str] = []

    async def unavailable(address: str) -> object:
        attempts.append(address)
        raise ConnectionError("Temporal unavailable")

    async def skip_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(temporal_worker.Client, "connect", unavailable)
    monkeypatch.setattr(temporal_worker.asyncio, "sleep", skip_sleep)

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await temporal_worker.start_temporal_worker("temporal:7233", "queue", object())

    assert attempts == ["temporal:7233"] * 3


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
async def test_run_worker_forever_always_starts_temporal_and_ignores_legacy_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired selector cannot make startup fall back to Postgres polling."""
    close_calls: list[str] = []
    outbound_clients = SimpleNamespace(
        telegram=_FakeAsyncClient("telegram", close_calls),
        webhook=_FakeAsyncClient("webhook", close_calls),
    )
    fake_service = object()
    worker_calls: list[dict[str, object]] = []

    async def start_temporal_worker(**kwargs: object) -> None:
        worker_calls.append(kwargs)

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
    monkeypatch.setattr(worker_main, "start_temporal_worker", start_temporal_worker)
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "legacy")
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

    await worker_main.run_worker_forever()

    assert worker_calls == [
        {
            "temporal_address": "temporal:7233",
            "task_queue": "task-execution-queue",
            "task_service": fake_service,
        }
    ]
    assert set(close_calls) == {"telegram", "webhook"}


@pytest.mark.anyio
async def test_run_worker_forever_bootstraps_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker runtime should invoke tracing bootstrap before Temporal startup."""
    close_calls: list[str] = []
    tracing_calls: list[str] = []
    outbound_clients = SimpleNamespace(
        telegram=_FakeAsyncClient("telegram", close_calls),
        webhook=_FakeAsyncClient("webhook", close_calls),
    )

    async def start_temporal_worker(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker_main, "should_run_worker", lambda: True)
    monkeypatch.setattr(worker_main, "create_outbound_http_clients", lambda: outbound_clients)
    monkeypatch.setattr(worker_main, "build_task_service_from_env", lambda **_: object())
    monkeypatch.setattr(worker_main, "start_temporal_worker", start_temporal_worker)
    monkeypatch.setattr(
        worker_main,
        "configure_tracing_from_env",
        lambda *, service_name: tracing_calls.append(service_name),
    )
    await worker_main.run_worker_forever()

    assert tracing_calls == ["code-agent-worker"]
    assert set(close_calls) == {"telegram", "webhook"}


def test_worker_main_calls_async_entrypoint_with_configured_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() should configure logging and delegate to asyncio.run."""
    logging_calls: list[dict[str, object]] = []
    run_calls: list[object] = []

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

    worker_main.main()

    assert logging_calls == [{"level": "DEBUG"}]
    assert len(run_calls) == 1
    run_calls[0].close()

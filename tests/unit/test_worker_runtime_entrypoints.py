"""Unit tests for worker runtime entrypoints and runtime mode helpers."""

from __future__ import annotations

import asyncio
import logging
import signal
from types import SimpleNamespace

import pytest

from apps import runtime as runtime_module
from apps.worker import main as worker_main
from db.enums import WorkerNodeStatus
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


def test_temporal_worker_identity_uses_stable_host_default(monkeypatch) -> None:
    """Container restarts should refresh one registry row unless explicitly overridden."""
    monkeypatch.delenv(temporal_worker.TEMPORAL_WORKER_ID_ENV_VAR, raising=False)
    monkeypatch.setattr(temporal_worker.socket, "gethostname", lambda: "worker-host")
    monkeypatch.setattr(temporal_worker.os, "getpid", lambda: 42)

    worker_id, process_identity = temporal_worker._temporal_worker_identity()

    assert worker_id == "worker-host"
    assert process_identity == "worker-host:42"


@pytest.mark.anyio
async def test_temporal_worker_registers_active_heartbeat_owner(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Service:
        def register_worker_node(self, **kwargs):
            calls.append(kwargs)
            return WorkerNodeStatus.ACTIVE

        async def _run_blocking(self, func, **kwargs):
            return func(**kwargs)

    monkeypatch.setattr(
        temporal_worker,
        "_temporal_worker_identity",
        lambda: ("worker-id", "worker-host:42"),
    )

    await temporal_worker._register_temporal_worker(Service(), worker_id="worker-id")

    assert calls == [
        {
            "worker_id": "worker-id",
            "capacity": 2,
            "process_identity": "worker-host:42",
        }
    ]


@pytest.mark.anyio
async def test_temporal_worker_heartbeat_failure_stops_runtime(monkeypatch) -> None:
    calls: list[str] = []

    class Service:
        def heartbeat_worker_node(self, *, worker_id: str):
            calls.append(worker_id)
            return None

        async def _run_blocking(self, func, **kwargs):
            return func(**kwargs)

    async def skip_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(temporal_worker.asyncio, "sleep", skip_sleep)

    with pytest.raises(RuntimeError, match="registry status is missing"):
        await temporal_worker._heartbeat_temporal_worker(Service(), worker_id="worker-id")

    assert calls == ["worker-id"]


@pytest.mark.anyio
async def test_temporal_worker_retries_transient_heartbeat_error(monkeypatch, caplog) -> None:
    calls: list[str] = []
    outcomes = iter((ConnectionError("database unavailable"), WorkerNodeStatus.OFFLINE))

    class Service:
        def heartbeat_worker_node(self, *, worker_id: str):
            calls.append(worker_id)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def _run_blocking(self, func, **kwargs):
            return func(**kwargs)

    async def skip_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(temporal_worker.asyncio, "sleep", skip_sleep)

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="offline"):
        await temporal_worker._heartbeat_temporal_worker(Service(), worker_id="worker-id")

    assert calls == ["worker-id", "worker-id"]
    assert "Temporal worker heartbeat failed; retrying" in caplog.text


@pytest.mark.parametrize(
    "registry_status",
    [WorkerNodeStatus.DRAINING, WorkerNodeStatus.OFFLINE, WorkerNodeStatus.QUARANTINED],
)
def test_temporal_worker_refuses_non_active_registry_status(registry_status) -> None:
    with pytest.raises(RuntimeError, match=registry_status.value):
        temporal_worker._require_active_worker(registry_status)


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
async def test_temporal_worker_groups_all_runtime_components(monkeypatch) -> None:
    """A component exit unwinds heartbeat, dispatch, and both Temporal worker loops."""
    calls: list[str] = []
    client = object()

    async def connect(address: str) -> object:
        calls.append(f"connect:{address}")
        return client

    async def register(_service: object, *, worker_id: str) -> None:
        calls.append(f"register:{worker_id}")

    async def heartbeat(_service: object, *, worker_id: str) -> None:
        calls.append(f"heartbeat:{worker_id}")

    class RuntimeWorker:
        def __init__(self, label: str) -> None:
            self.label = label

        async def run(self) -> None:
            calls.append(self.label)

    class Dispatcher:
        def __init__(self, *, client: object, session_factory: object) -> None:
            assert client is not None
            assert session_factory == "sessions"

        async def run_forever(self) -> None:
            calls.append("dispatcher")

    monkeypatch.setattr(temporal_worker.Client, "connect", connect)
    monkeypatch.setattr(temporal_worker, "_register_temporal_worker", register)
    monkeypatch.setattr(temporal_worker, "_heartbeat_temporal_worker", heartbeat)
    monkeypatch.setattr(
        temporal_worker,
        "_temporal_worker_identity",
        lambda: ("worker-id", "host:1"),
    )
    monkeypatch.setattr(
        temporal_worker,
        "_build_temporal_workers",
        lambda **_kwargs: (RuntimeWorker("workflow"), RuntimeWorker("execution")),
    )
    monkeypatch.setattr(temporal_worker, "TemporalCommandDispatcher", Dispatcher)

    await temporal_worker.start_temporal_worker(
        "temporal:7233",
        "task-queue",
        SimpleNamespace(session_factory="sessions"),
    )

    assert calls == [
        "connect:temporal:7233",
        "register:worker-id",
        "heartbeat:worker-id",
        "dispatcher",
        "workflow",
        "execution",
    ]


def test_temporal_worker_builds_workflow_and_bounded_execution_workers(monkeypatch) -> None:
    """Both Temporal queues share activities while execution concurrency stays bounded."""
    created: list[dict[str, object]] = []

    class Activities:
        def __getattr__(self, name: str) -> str:
            return name

    def build_worker(client: object, **kwargs: object) -> SimpleNamespace:
        created.append({"client": client, **kwargs})
        return SimpleNamespace(config=kwargs)

    monkeypatch.setattr(
        temporal_worker,
        "TaskExecutionActivities",
        lambda *, service: Activities(),
    )
    monkeypatch.setattr(temporal_worker, "Worker", build_worker)
    monkeypatch.setattr(temporal_worker, "UnsandboxedWorkflowRunner", lambda: "runner")

    workflow, execution = temporal_worker._build_temporal_workers(
        client=object(),
        task_queue="workflow-queue",
        task_service=object(),
    )

    assert workflow.config["task_queue"] == "workflow-queue"
    assert workflow.config["workflow_runner"] == "runner"
    assert len(workflow.config["activities"]) == 17
    assert "persist_rejected_session_state" in workflow.config["activities"]
    assert execution.config["task_queue"] == temporal_worker.CODEX_EXECUTION_TASK_QUEUE
    assert execution.config["activities"] == ["run_worker", "run_decomposed_node"]
    assert execution.config["max_concurrent_activities"] == 2
    assert len(created) == 2


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


@pytest.mark.anyio
async def test_worker_sigterm_cancels_runtime_and_removes_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM should unwind the runtime so its cleanup can complete."""
    actual_loop = asyncio.get_running_loop()
    registered_handlers: dict[signal.Signals, object] = {}
    removed_signals: list[signal.Signals] = []
    cleanup_complete = asyncio.Event()

    class FakeLoop:
        def add_signal_handler(self, sig, callback) -> None:
            registered_handlers[sig] = callback
            actual_loop.call_soon(callback)

        def remove_signal_handler(self, sig) -> bool:
            removed_signals.append(sig)
            return True

    async def run_until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_complete.set()

    monkeypatch.setattr(worker_main.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(worker_main, "run_worker_forever", run_until_cancelled)

    await worker_main.run_worker_until_stopped()

    assert signal.SIGTERM in registered_handlers
    assert cleanup_complete.is_set()
    assert removed_signals == [signal.SIGTERM]


@pytest.mark.anyio
async def test_worker_shutdown_coordinator_preserves_runtime_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-signal worker failures must still terminate the process visibly."""

    class FakeLoop:
        def add_signal_handler(self, _sig, _callback) -> None:
            return None

        def remove_signal_handler(self, _sig) -> bool:
            return True

    async def fail_startup() -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(worker_main.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(worker_main, "run_worker_forever", fail_startup)

    with pytest.raises(RuntimeError, match="startup failed"):
        await worker_main.run_worker_until_stopped()


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

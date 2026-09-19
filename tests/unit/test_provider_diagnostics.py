"""Unit tests for provider pre-dispatch diagnostics service and checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from db.base import utc_now
from orchestrator.provider_diagnostics import (
    ProviderDiagnosticsService,
    clear_probe_cache,
    compute_report_id,
    resolve_execution_context,
)
from orchestrator.provider_diagnostics_types import (
    DiagnosticCheckResult,
    ProviderExecutionContext,
)
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretScope,
    SecretSource,
)
from workers.base import WorkerRequest


@pytest.fixture(autouse=True)
def reset_probe_cache() -> None:
    clear_probe_cache()
    yield
    clear_probe_cache()


def _make_context(
    *,
    provider: str = "codex",
    auth_mechanism: str = "chatgpt_oauth",
    credential_refs: tuple[str, ...] = (),
    is_container_execution: bool = True,
    executable: str = "codex",
    executor_image: str = "code-agent-worker",
    logical_execution_key: str = "test-task:attempt:1:primary",
) -> ProviderExecutionContext:
    return ProviderExecutionContext(
        provider=provider,
        worker_profile="default",
        runtime_mode="native_agent",
        model="gpt-5.6-luna",
        reasoning_effort="high",
        executable=executable,
        executor_image=executor_image,
        auth_mechanism=auth_mechanism,  # type: ignore[arg-type]
        credential_refs=credential_refs,
        is_container_execution=is_container_execution,
        task_id="test-task",
        session_id="test-session",
        attempt_count=1,
        logical_execution_key=logical_execution_key,
    )


def test_compute_report_id_is_deterministic() -> None:
    id1 = compute_report_id("task-1:node-A:attempt:1")
    id2 = compute_report_id("task-1:node-A:attempt:1")
    id3 = compute_report_id("task-1:node-B:attempt:1")
    assert id1 == id2
    assert id1 != id3
    assert len(id1) == 32


def test_resolve_execution_context_codex_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request = WorkerRequest(
        task_text="Inspect code",
        secret_refs=[SecretRef(name="openai_api_key")],
        runtime_manifest={
            "worker": {"worker_profile": "codex-native", "runtime_mode": "native_agent"},
            "sandbox": {"native_executor_image": "test-worker-image"},
        },
    )
    ctx = resolve_execution_context(None, request, task_id="t-1", attempt_count=2)
    assert ctx.provider == "unknown" or ctx.runtime_mode == "native_agent"
    assert ctx.auth_mechanism == "none"

    class FakeCodexWorker:
        worker_type = "codex"
        default_runtime_mode = "native_agent"

    ctx2 = resolve_execution_context(FakeCodexWorker(), request, task_id="t-1", attempt_count=2)
    assert ctx2.provider == "codex"
    assert ctx2.auth_mechanism == "api_key"
    assert "openai_api_key" in ctx2.credential_refs
    assert ctx2.executor_image == "test-worker-image"


def test_resolve_execution_context_honors_request_image_for_persistent_sandbox() -> None:
    worker = SimpleNamespace(
        worker_type="openrouter",
        default_runtime_mode="tool_loop",
        container_manager=SimpleNamespace(default_image="worker-default:latest"),
        runtime_adapter=SimpleNamespace(api_key="sk-openrouter-test"),
    )
    request = WorkerRequest(
        task_text="Inspect code",
        runtime_mode="tool_loop",
        image="request-image:latest",
    )
    custom = resolve_execution_context(worker, request, worker_type="openrouter")
    assert custom.executor_image == "request-image:latest"

    default = resolve_execution_context(
        worker,
        request.model_copy(update={"image": None}),
        worker_type="openrouter",
    )
    assert default.executor_image == "worker-default:latest"


def test_resolve_execution_context_native_image_does_not_use_request_image() -> None:
    worker = SimpleNamespace(
        worker_type="codex",
        default_runtime_mode="native_agent",
        container_manager=SimpleNamespace(default_image="worker-default:latest"),
        runtime_adapter=SimpleNamespace(auth_mode="chatgpt_oauth"),
    )
    request = WorkerRequest(
        task_text="Inspect code",
        runtime_mode="native_agent",
        image="request-image:latest",
        runtime_manifest={"sandbox": {"native_executor_image": "native-image:latest"}},
    )
    context = resolve_execution_context(worker, request, worker_type="codex")
    assert context.executor_image == "native-image:latest"


def test_codex_api_key_registered_with_nonempty_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_api_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_api_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "ready"
    assert not res.blocking


def test_codex_api_key_registered_with_empty_source_is_unready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_api_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_api_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "source is empty" in res.detail


def test_codex_openai_key_alias_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="OPENAI_API_KEY",
        )
    )
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "ready"


def test_codex_api_key_rejects_mismatched_destination() -> None:
    registry = SecretRegistry(
        [
            RegisteredSecretDefinition(
                name="misrouted_openai_key",
                source=SecretSource.ENV,
                source_key="OPENAI_API_KEY",
                required_scope=SecretScope.PROVIDER_AUTH,
                exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
                destination_env_var="OPENROUTER_API_KEY",
            )
        ]
    )
    svc = ProviderDiagnosticsService(
        secret_registry=registry,
        secret_env={"OPENAI_API_KEY": "sk-openai-test"},
    )

    res = svc.check_credentials(
        _make_context(auth_mechanism="api_key", credential_refs=("misrouted_openai_key",))
    )

    assert res.status == "unready"
    assert res.blocking
    assert "wrong environment variable" in res.detail


def test_codex_api_key_rejects_file_only_delivery() -> None:
    registry = SecretRegistry(
        [
            RegisteredSecretDefinition(
                name="file_only_openai_key",
                source=SecretSource.FILE,
                source_key="OPENAI_API_KEY",
                required_scope=SecretScope.PROVIDER_AUTH,
                exposure_policy=SecretExposurePolicy.SANDBOX_FILE,
                destination_mount_name="openai-api-key",
            )
        ]
    )
    svc = ProviderDiagnosticsService(secret_registry=registry)

    res = svc.check_credentials(
        _make_context(auth_mechanism="api_key", credential_refs=("file_only_openai_key",))
    )

    assert res.status == "unready"
    assert res.blocking
    assert "file-only" in res.detail


def test_codex_api_key_non_environment_source_is_unknown_until_resolvable() -> None:
    definition = RegisteredSecretDefinition(
        name="stored_openai_key",
        source=SecretSource.SECRET_STORE,
        source_key="provider/openai",
        required_scope=SecretScope.PROVIDER_AUTH,
        exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
        destination_env_var="OPENAI_API_KEY",
    )
    registry = SecretRegistry([definition])
    context = _make_context(auth_mechanism="api_key", credential_refs=(definition.name,))

    unknown = ProviderDiagnosticsService(secret_registry=registry).check_credentials(context)
    resolved = ProviderDiagnosticsService(
        secret_registry=registry,
        secret_store={"provider/openai": "sk-openai-test"},
    ).check_credentials(context)

    assert unknown.status == "unknown"
    assert unknown.blocking
    assert "not locally inspectable" in unknown.detail
    assert resolved.status == "ready"


@pytest.mark.parametrize(
    ("required_scope", "exposure_policy", "expected_detail"),
    [
        (SecretScope.CUSTOM, SecretExposurePolicy.SANDBOX_ENV, "lacks provider_auth scope"),
        (SecretScope.PROVIDER_AUTH, SecretExposurePolicy.BROKER_ONLY, "not exposed"),
    ],
)
def test_codex_registered_api_key_rejects_unsafe_definition(
    monkeypatch: pytest.MonkeyPatch,
    required_scope: SecretScope,
    exposure_policy: SecretExposurePolicy,
    expected_detail: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    registry = SecretRegistry()
    registry.register(
        RegisteredSecretDefinition(
            name="openai_api_key",
            source=SecretSource.ENV,
            source_key="OPENAI_API_KEY",
            required_scope=required_scope,
            exposure_policy=exposure_policy,
            destination_env_var=(
                "OPENAI_API_KEY" if exposure_policy == SecretExposurePolicy.SANDBOX_ENV else None
            ),
        )
    )
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_api_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert expected_detail in res.detail


def test_codex_api_key_unregistered_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = SecretRegistry()
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_api_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "missing from SecretRegistry" in res.detail


def test_codex_oauth_missing_auth_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty_dir = tmp_path / "codex_auth"
    empty_dir.mkdir()
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(empty_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(auth_mechanism="chatgpt_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "auth.json not found" in res.detail
    assert "docker compose run --rm --no-deps worker codex login" in (res.remediation or "")


def test_codex_oauth_valid_auth_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_dir = tmp_path / "codex_auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text('{"token": "test-token"}', encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(auth_mechanism="chatgpt_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "ready"
    assert not res.blocking
    assert res.verification_scope == "local_structure"


def test_codex_oauth_malformed_auth_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_dir = tmp_path / "codex_auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text("NOT_VALID_JSON{", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(auth_mechanism="chatgpt_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "malformed or unreadable" in res.detail


def test_antigravity_with_only_oauth_creds_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_dir = tmp_path / "gemini_auth"
    auth_dir.mkdir()
    (auth_dir / "oauth_creds.json").write_text('{"client_id": "123"}', encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="antigravity", auth_mechanism="antigravity_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert (
        "Found oauth_creds.json, but native Antigravity requires antigravity-oauth-token"
        in res.detail
    )


def test_antigravity_valid_oauth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_dir = tmp_path / "gemini_auth"
    cli_dir = auth_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text("oauth-token-secret-data", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="antigravity", auth_mechanism="antigravity_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "ready"
    assert not res.blocking
    assert res.verification_scope == "local_structure"


def test_openrouter_credentials_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="openrouter", auth_mechanism="api_key")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "OpenRouter API key is not configured" in res.detail


def test_openrouter_ignores_unrelated_task_secret_when_adapter_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivery credentials must not override the provider's configured API key."""
    from workers.openrouter_adapter import OpenRouterCliRuntimeAdapter

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", lambda **_kwargs: object())
    adapter = OpenRouterCliRuntimeAdapter(api_key="sk-openrouter-configured")
    worker = SimpleNamespace(worker_type="openrouter", runtime_adapter=adapter)
    svc = ProviderDiagnosticsService(
        worker=worker,
        secret_registry=SecretRegistry(
            [
                RegisteredSecretDefinition(
                    name="github_token",
                    source=SecretSource.ENV,
                    source_key="GITHUB_TOKEN",
                    required_scope=SecretScope.GIT_PUSH,
                    exposure_policy=SecretExposurePolicy.BROKER_ONLY,
                )
            ]
        ),
        secret_env={},
    )

    res = svc.check_credentials(
        _make_context(
            provider="openrouter",
            auth_mechanism="api_key",
            credential_refs=("github_token",),
        )
    )

    assert res.status == "ready"
    assert not res.blocking


def test_openrouter_explicit_invalid_provider_reference_fails_closed() -> None:
    registry = SecretRegistry(
        [
            RegisteredSecretDefinition(
                name="invalid_openrouter_key",
                source=SecretSource.ENV,
                source_key="OPENROUTER_API_KEY",
                required_scope=SecretScope.PROVIDER_AUTH,
                exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
                destination_env_var="OPENAI_API_KEY",
            )
        ]
    )
    svc = ProviderDiagnosticsService(
        secret_registry=registry,
        secret_env={"OPENROUTER_API_KEY": "sk-openrouter-test"},
        effective_api_key_configured=True,
    )

    res = svc.check_credentials(
        _make_context(
            provider="openrouter",
            auth_mechanism="api_key",
            credential_refs=("invalid_openrouter_key",),
        )
    )

    assert res.status == "unready"
    assert res.blocking
    assert "wrong environment variable" in res.detail


def test_openrouter_ignores_explicit_openai_reference() -> None:
    registry = SecretRegistry(
        [
            RegisteredSecretDefinition(
                name="openai_api_key",
                source=SecretSource.ENV,
                source_key="OPENAI_API_KEY",
                required_scope=SecretScope.PROVIDER_AUTH,
                exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
                destination_env_var="OPENAI_API_KEY",
            )
        ]
    )
    svc = ProviderDiagnosticsService(
        secret_registry=registry,
        secret_env={"OPENROUTER_API_KEY": "sk-openrouter-test"},
        effective_api_key_configured=True,
    )

    res = svc.check_credentials(
        _make_context(
            provider="openrouter",
            auth_mechanism="api_key",
            credential_refs=("openai_api_key",),
        )
    )

    assert res.status == "ready"
    assert not res.blocking


def test_openrouter_skips_cli_check() -> None:
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="openrouter", auth_mechanism="api_key", executable=None)
    res = svc.check_cli_binary(ctx)
    assert res.status == "ready"
    assert not res.blocking
    assert "CLI binary check skipped" in res.detail


@pytest.mark.asyncio
async def test_docker_daemon_responsive() -> None:
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_probe_docker_process", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = (0, "24.0.7", "")
        res = await svc.check_docker_daemon()
        assert res.status == "ready"
        assert not res.blocking
        assert "version 24.0.7" in res.detail


@pytest.mark.asyncio
async def test_docker_daemon_timeout_is_blocking() -> None:
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_probe_docker_process", new_callable=AsyncMock) as mock_probe:
        mock_probe.side_effect = TimeoutError("docker info timeout")
        res = await svc.check_docker_daemon()
        assert res.status == "unknown"
        assert res.blocking
        assert "timed out" in res.detail


@pytest.mark.asyncio
async def test_executor_image_missing_is_blocking() -> None:
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_inspect_image_process", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.return_value = (1, "", "Error: No such image")
        res = await svc.check_executor_image("nonexistent-image:latest")
        assert res.status == "unready"
        assert res.blocking
        assert "was not found locally" in res.detail
        assert "docker compose build worker" in (res.remediation or "")


@pytest.mark.asyncio
async def test_evaluate_pre_dispatch_rejection_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty_codex"
    empty_dir.mkdir()
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(empty_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="codex", auth_mechanism="chatgpt_oauth")

    with patch.object(svc, "check_docker_daemon", new_callable=AsyncMock) as mock_docker:
        mock_docker.return_value = DiagnosticCheckResult(
            name="docker_daemon",
            category="container_runtime",
            status="ready",
            detail="Docker is up",
            verification_scope="local_runtime",
            blocking=False,
        )
        report = await svc.evaluate_pre_dispatch(ctx)
        assert not report.ready
        assert report.decision == "preflight_rejected"
        assert not report.execution_started
        assert report.failure_kind == "provider_auth"
        assert report.next_action_hint == "configure_codex_auth"
        assert "PRE_DISPATCH_DIAGNOSTIC_FAILURE" in (report.summary or "")


@pytest.mark.asyncio
async def test_evaluate_pre_dispatch_cancellation_propagates() -> None:
    svc = ProviderDiagnosticsService()
    ctx = _make_context()
    with patch.object(svc, "check_docker_daemon", new_callable=AsyncMock) as mock_docker:
        mock_docker.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await svc.evaluate_pre_dispatch(ctx)


@pytest.mark.asyncio
async def test_evaluate_all_providers_respects_required() -> None:
    svc = ProviderDiagnosticsService(
        workers={
            "codex": SimpleNamespace(
                worker_type="codex",
                default_runtime_mode="native_agent",
                container_manager=SimpleNamespace(default_image="worker:latest"),
            )
        }
    )
    with (
        patch.object(svc, "check_credentials") as mock_cred,
        patch.object(svc, "check_docker_daemon", new_callable=AsyncMock) as mock_docker,
        patch.object(svc, "check_executor_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_docker.return_value = DiagnosticCheckResult(
            name="docker_daemon",
            category="container_runtime",
            status="ready",
            detail="up",
            verification_scope="local_runtime",
            blocking=False,
        )
        mock_img.return_value = DiagnosticCheckResult(
            name="executor_image",
            category="container_runtime",
            status="ready",
            detail="available",
            verification_scope="local_runtime",
            blocking=False,
        )

        def cred_side_effect(ctx: ProviderExecutionContext) -> DiagnosticCheckResult:
            if ctx.provider == "codex":
                return DiagnosticCheckResult(
                    name="credentials",
                    category="credentials",
                    status="ready",
                    detail="Codex ok",
                    verification_scope="local_structure",
                    blocking=False,
                )
            return DiagnosticCheckResult(
                name="credentials",
                category="credentials",
                status="unready",
                detail=f"{ctx.provider} missing",
                verification_scope="local_presence",
                blocking=True,
            )

        mock_cred.side_effect = cred_side_effect
        report = await svc.evaluate_all_providers(required_providers={"codex"})
        assert report.providers["codex"].ready
        assert not report.providers["antigravity"].ready
        assert not report.all_ready
        assert report.required_providers_ready


@pytest.mark.asyncio
async def test_evaluate_all_providers_uses_openrouter_worker_container_contract() -> None:
    from workers.facade import WorkerFacade

    worker = SimpleNamespace(
        default_runtime_mode="tool_loop",
        runtime_adapter=SimpleNamespace(api_key="sk-openrouter-test"),
        container_manager=SimpleNamespace(default_image="openrouter-worker:latest"),
    )
    svc = ProviderDiagnosticsService(
        worker=WorkerFacade(openrouter_worker=worker),
        secret_env={},
    )
    docker_unavailable = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="unready",
        detail="Docker daemon unavailable.",
        verification_scope="local_runtime",
        blocking=True,
    )

    with patch.object(
        svc,
        "check_docker_daemon",
        new_callable=AsyncMock,
        return_value=docker_unavailable,
    ) as mock_docker:
        report = await svc.evaluate_all_providers(
            required_providers={"openrouter"},
            target_providers={"openrouter"},
        )

    assert not report.providers["openrouter"].ready
    assert not report.required_providers_ready
    mock_docker.assert_awaited_once()
    assert any(check.name == "docker_daemon" for check in report.providers["openrouter"].checks)


@pytest.mark.asyncio
async def test_workerless_operator_probe_is_limited_and_not_ready() -> None:
    svc = ProviderDiagnosticsService(secret_env={"OPENROUTER_API_KEY": "sk-openrouter-test"})
    ready_docker = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="ready",
        detail="Docker daemon is responsive.",
        verification_scope="local_runtime",
        blocking=False,
    )
    ready_image = DiagnosticCheckResult(
        name="executor_image",
        category="container_runtime",
        status="ready",
        detail="Executor image is available.",
        verification_scope="local_runtime",
        blocking=False,
    )

    with (
        patch.object(svc, "check_docker_daemon", new_callable=AsyncMock, return_value=ready_docker),
        patch.object(svc, "check_executor_image", new_callable=AsyncMock, return_value=ready_image),
    ):
        report = await svc.evaluate_all_providers(
            required_providers={"openrouter"},
            target_providers={"openrouter"},
        )

    openrouter = report.providers["openrouter"]
    assert not openrouter.ready
    assert not report.required_providers_ready
    assert "LIMITED_HOST_PROBE" in (openrouter.summary or "")
    assert any(check.name == "worker_configuration" for check in openrouter.checks)


@pytest.mark.asyncio
async def test_evaluate_all_providers_restricts_targets_and_invalid_required_is_unready() -> None:
    svc = ProviderDiagnosticsService(
        workers={
            "codex": SimpleNamespace(
                worker_type="codex",
                default_runtime_mode="native_agent",
                container_manager=SimpleNamespace(default_image="worker:latest"),
            )
        }
    )
    ready = DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail="ok",
        verification_scope="local_presence",
        blocking=False,
    )
    with (
        patch.object(svc, "check_credentials", return_value=ready),
        patch.object(svc, "check_docker_daemon", new_callable=AsyncMock) as mock_docker,
        patch.object(svc, "check_executor_image", new_callable=AsyncMock) as mock_image,
    ):
        mock_docker.return_value = DiagnosticCheckResult(
            name="docker_daemon",
            category="container_runtime",
            status="ready",
            detail="up",
            verification_scope="local_runtime",
            blocking=False,
        )
        mock_image.return_value = DiagnosticCheckResult(
            name="executor_image",
            category="container_runtime",
            status="ready",
            detail="available",
            verification_scope="local_runtime",
            blocking=False,
        )
        report = await svc.evaluate_all_providers(
            target_providers={"codex"},
            required_providers={"nonexistent"},
        )

    assert set(report.providers) == {"codex"}
    assert report.all_ready
    assert not report.required_providers_ready


# ---------------------------------------------------------------------------
# Additional coverage tests — provider_diagnostics.py
# ---------------------------------------------------------------------------


# ---- _resolve_executable / _resolve_auth_mechanism --------------------------


def test_resolve_execution_context_antigravity_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Line 57: antigravity executable resolved via env var."""
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_CLI_BIN", "myantigravity")

    class FakeAGYWorker:
        worker_type = "antigravity"
        default_runtime_mode = "native_agent"

    request = WorkerRequest(task_text="Hello")
    ctx = resolve_execution_context(FakeAGYWorker(), request, task_id="t1")
    assert ctx.executable == "myantigravity"


def test_resolve_auth_mechanism_antigravity() -> None:
    """Line 70: antigravity worker_type -> antigravity_oauth auth mechanism."""

    class FakeAGYWorker:
        worker_type = "antigravity"
        default_runtime_mode = "native_agent"

    request = WorkerRequest(task_text="Hello")
    ctx = resolve_execution_context(FakeAGYWorker(), request, task_id="t1")
    assert ctx.auth_mechanism == "antigravity_oauth"


def test_resolve_auth_mechanism_openrouter() -> None:
    """Line 72: openrouter worker_type -> api_key auth mechanism."""

    class FakeORWorker:
        worker_type = "openrouter"
        default_runtime_mode = "native_agent"

    request = WorkerRequest(task_text="Hello")
    ctx = resolve_execution_context(FakeORWorker(), request, task_id="t1")
    assert ctx.auth_mechanism == "api_key"


def test_resolve_execution_context_uses_concrete_container_backend() -> None:
    class _ContainerManager:
        default_image = "sandbox-openrouter:test"

    class _OpenRouterWorker:
        worker_type = "openrouter"
        default_runtime_mode = "tool_loop"
        container_manager = _ContainerManager()

    request = WorkerRequest(
        task_text="Hello",
        runtime_mode="tool_loop",
        runtime_manifest={"sandbox": {"native_executor_image": "native-agent:test"}},
    )
    ctx = resolve_execution_context(_OpenRouterWorker(), request, task_id="t-openrouter")
    assert ctx.is_container_execution
    assert ctx.executor_image == "sandbox-openrouter:test"


def test_codex_tool_loop_with_concrete_container_backend_requires_docker() -> None:
    class _ContainerManager:
        default_image = "sandbox-codex:test"

    class _CodexWorker:
        worker_type = "codex"
        default_runtime_mode = "tool_loop"
        container_manager = _ContainerManager()

    request = WorkerRequest(task_text="Hello", runtime_mode="tool_loop")
    ctx = resolve_execution_context(_CodexWorker(), request, task_id="t-codex")
    assert ctx.is_container_execution
    assert ctx.runtime_mode == "tool_loop"


@pytest.mark.asyncio
async def test_concrete_container_backends_probe_docker_when_unavailable() -> None:
    class _ContainerManager:
        default_image = "sandbox:test"

    class _Worker:
        def __init__(self, worker_type: str) -> None:
            self.worker_type = worker_type
            self.default_runtime_mode = "tool_loop"
            self.container_manager = _ContainerManager()

    ready_credentials = DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="ready",
        detail="ok",
        verification_scope="local_presence",
        blocking=False,
    )
    docker_unready = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="unready",
        detail="Docker unavailable",
        verification_scope="local_runtime",
        blocking=True,
    )
    service = ProviderDiagnosticsService()
    with (
        patch.object(service, "check_credentials", return_value=ready_credentials),
        patch.object(service, "check_docker_daemon", new_callable=AsyncMock) as mock_docker,
    ):
        mock_docker.return_value = docker_unready
        for worker_type in ("openrouter", "codex"):
            worker = _Worker(worker_type)
            request = WorkerRequest(
                task_text="Hello",
                worker_type=worker_type,  # type: ignore[arg-type]
                runtime_mode="tool_loop",
            )
            context = resolve_execution_context(worker, request, worker_type=worker_type)
            report = await service.evaluate_pre_dispatch(context)
            assert context.is_container_execution
            assert not report.ready
            assert any(check.name == "docker_daemon" for check in report.checks)

    assert mock_docker.await_count == 2


# ---- check_docker_daemon uncovered branches ----------------------------------


@pytest.mark.asyncio
async def test_docker_daemon_cli_missing() -> None:
    """Lines 208-219: shutil.which('docker') returns None -> unready."""
    svc = ProviderDiagnosticsService()
    with patch("orchestrator.provider_diagnostics.shutil.which", return_value=None):
        res = await svc.check_docker_daemon()
    assert res.status == "unready"
    assert res.blocking
    assert "not found in host PATH" in res.detail


@pytest.mark.asyncio
async def test_docker_daemon_cached_result() -> None:
    """Line 206: second call returns cached result without re-probing."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_probe_docker_process", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = (0, "24.0.7", "")
        res1 = await svc.check_docker_daemon()
        res2 = await svc.check_docker_daemon()
    assert mock_probe.call_count == 1
    assert res1.status == res2.status == "ready"


@pytest.mark.asyncio
async def test_docker_daemon_nonzero_exit() -> None:
    """Lines 233-234: Docker probe returns non-zero exit code -> unready."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_probe_docker_process", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = (1, "", "Cannot connect to the Docker daemon")
        res = await svc.check_docker_daemon()
    assert res.status == "unready"
    assert res.blocking
    assert "Docker daemon unavailable" in res.detail


@pytest.mark.asyncio
async def test_docker_daemon_generic_exception() -> None:
    """Lines 255-256: Unexpected OSError -> unready with exception name in detail."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_probe_docker_process", new_callable=AsyncMock) as mock_probe:
        mock_probe.side_effect = OSError("socket permission denied")
        res = await svc.check_docker_daemon()
    assert res.status == "unready"
    assert res.blocking
    assert "OSError" in res.detail


# ---- _inspect_image_process timeout (lines 283-289) -------------------------


@pytest.mark.asyncio
async def test_inspect_image_process_propagates_timeout() -> None:
    """Lines 283-289: TimeoutError kills subprocess and re-raises."""
    import asyncio

    svc = ProviderDiagnosticsService(docker_probe_timeout=0.001)

    class _FakeProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return -1

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mk:
        mk.return_value = _FakeProc()
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await svc._inspect_image_process("slow:latest")


# ---- check_executor_image uncovered branches ---------------------------------


@pytest.mark.asyncio
async def test_check_executor_image_cached() -> None:
    """Line 296: second call returns cached result without re-inspecting."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_inspect_image_process", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.return_value = (0, "[]", "")
        await svc.check_executor_image("my-image:latest")
        await svc.check_executor_image("my-image:latest")
    assert mock_inspect.call_count == 1


@pytest.mark.asyncio
async def test_check_executor_image_timeout() -> None:
    """Lines 319-331: TimeoutError during image inspection -> unknown status."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_inspect_image_process", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.side_effect = TimeoutError("inspect timed out")
        res = await svc.check_executor_image("slow-image:latest")
    assert res.status == "unknown"
    assert res.blocking
    assert "timed out" in res.detail


@pytest.mark.asyncio
async def test_check_executor_image_generic_exception() -> None:
    """Lines 332-341: Generic OSError during image inspection -> unready."""
    svc = ProviderDiagnosticsService()
    with patch.object(svc, "_inspect_image_process", new_callable=AsyncMock) as mock_inspect:
        mock_inspect.side_effect = OSError("permission denied")
        res = await svc.check_executor_image("broken-image:latest")
    assert res.status == "unready"
    assert res.blocking
    assert "OSError" in res.detail


# ---- check_cli_binary host-PATH branches (lines 372-390) --------------------


def test_check_cli_binary_host_executable_not_found() -> None:
    """Lines 372-381: non-container execution; executable not on host PATH -> unready."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context(is_container_execution=False, executable="missing-bin")
    with patch("orchestrator.provider_diagnostics.shutil.which", return_value=None):
        res = svc.check_cli_binary(ctx)
    assert res.status == "unready"
    assert res.blocking
    assert "not found on host PATH" in res.detail


def test_check_cli_binary_host_executable_found() -> None:
    """Lines 383-390: non-container execution; executable on host PATH -> ready."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context(is_container_execution=False, executable="codex")
    with patch("orchestrator.provider_diagnostics.shutil.which", return_value="/usr/bin/codex"):
        res = svc.check_cli_binary(ctx)
    assert res.status == "ready"
    assert not res.blocking
    assert "found on host PATH" in res.detail


# ---- _build_rejection_report hint branches (lines 409, 411) -----------------


def test_build_rejection_report_docker_hint() -> None:
    """Line 409: docker_daemon primary -> start_docker_daemon hint."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context()
    primary = DiagnosticCheckResult(
        name="docker_daemon",
        category="container_runtime",
        status="unready",
        detail="Docker is down",
        verification_scope="local_runtime",
        blocking=True,
    )
    report = svc._build_rejection_report("rid", utc_now(), ctx, [primary], primary)
    assert report.next_action_hint == "start_docker_daemon"
    assert report.failure_kind == "sandbox_infra"


def test_build_rejection_report_executor_image_hint() -> None:
    """Line 411: executor_image primary -> build_or_pull_executor_image hint."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context()
    primary = DiagnosticCheckResult(
        name="executor_image",
        category="container_runtime",
        status="unready",
        detail="Image missing",
        verification_scope="local_runtime",
        blocking=True,
    )
    report = svc._build_rejection_report("rid", utc_now(), ctx, [primary], primary)
    assert report.next_action_hint == "build_or_pull_executor_image"


def test_build_rejection_report_antigravity_hint() -> None:
    """Lines 415-416: credentials + antigravity -> bootstrap_antigravity_auth hint."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="antigravity")
    primary = DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="unready",
        detail="Token missing",
        verification_scope="local_presence",
        blocking=True,
    )
    report = svc._build_rejection_report("rid", utc_now(), ctx, [primary], primary)
    assert report.next_action_hint == "bootstrap_antigravity_auth"
    assert report.failure_kind == "provider_auth"


def test_build_rejection_report_openrouter_hint() -> None:
    """Lines 417-418: credentials + openrouter -> configure_openrouter_api_key hint."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="openrouter", auth_mechanism="api_key")
    primary = DiagnosticCheckResult(
        name="credentials",
        category="credentials",
        status="unready",
        detail="API key missing",
        verification_scope="local_presence",
        blocking=True,
    )
    report = svc._build_rejection_report("rid", utc_now(), ctx, [primary], primary)
    assert report.next_action_hint == "configure_openrouter_api_key"


# ---- evaluate_pre_dispatch internal exception (lines 480-482) ---------------


@pytest.mark.asyncio
async def test_evaluate_pre_dispatch_internal_exception() -> None:
    """Lines 480-482: unexpected RuntimeError returns preflight_error report."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context()
    with patch.object(svc, "check_credentials", side_effect=RuntimeError("boom")):
        report = await svc.evaluate_pre_dispatch(ctx)
    assert not report.ready
    assert report.decision == "preflight_error"
    assert report.reason_code == "preflight_internal_error"
    assert "RuntimeError" in (report.summary or "")


# ---------------------------------------------------------------------------
# Additional coverage tests — provider_diagnostics_credentials.py
# ---------------------------------------------------------------------------


def test_resolve_codex_auth_path_codex_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 23: CODEX_HOME used when CODE_AGENT_CODEX_AUTH_DIR is unset."""
    from orchestrator.provider_diagnostics_credentials import resolve_codex_auth_path

    monkeypatch.delenv("CODE_AGENT_CODEX_AUTH_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex_home")
    path = resolve_codex_auth_path()
    assert path == Path("/tmp/codex_home/auth.json")


def test_resolve_antigravity_token_path_gemini_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 32-35: GEMINI_HOME used when CODE_AGENT_ANTIGRAVITY_AUTH_DIR is unset."""
    from orchestrator.provider_diagnostics_credentials import resolve_antigravity_token_path

    monkeypatch.delenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", raising=False)
    monkeypatch.setenv("GEMINI_HOME", "/tmp/gemini_home")
    path = resolve_antigravity_token_path()
    assert path == Path("/tmp/gemini_home/antigravity-cli/antigravity-oauth-token")


def test_codex_api_key_ref_true_reg_def_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 57: has_ref=True but reg_def=None -> unready (missing from SecretRegistry)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    registry = SecretRegistry()
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(auth_mechanism="api_key", credential_refs=("openai_api_key",))
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "missing from SecretRegistry" in res.detail


def test_codex_oauth_empty_auth_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 92: auth.json exists but is empty -> malformed error."""
    auth_dir = tmp_path / "codex_auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(auth_mechanism="chatgpt_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "malformed or unreadable" in res.detail


def test_codex_oauth_preflight_uses_fallback_directory(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".codex"
    fallback = tmp_path / "fallback" / ".codex"
    configured.mkdir(parents=True)
    fallback.mkdir(parents=True)
    (fallback / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(configured))
    monkeypatch.setenv("CODEX_HOME", str(fallback))

    result = ProviderDiagnosticsService().check_credentials(
        _make_context(auth_mechanism="chatgpt_oauth")
    )

    assert result.status == "ready"


def test_antigravity_token_missing_no_oauth_creds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line 138: token absent and oauth_creds.json also absent -> plain unready."""
    auth_dir = tmp_path / "gemini_auth"
    auth_dir.mkdir()
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="antigravity", auth_mechanism="antigravity_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "Antigravity OAuth token not found" in res.detail


def test_antigravity_preflight_uses_fallback_directory(tmp_path: Path, monkeypatch) -> None:
    configured = tmp_path / "configured" / ".gemini"
    fallback = tmp_path / "fallback" / ".gemini"
    configured.mkdir(parents=True)
    token_path = fallback / "antigravity-cli/antigravity-oauth-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("token", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(configured))
    monkeypatch.delenv("GEMINI_HOME", raising=False)
    monkeypatch.setenv("CODE_AGENT_GEMINI_AUTH_DIR", str(fallback))

    result = ProviderDiagnosticsService().check_credentials(
        _make_context(provider="antigravity", auth_mechanism="antigravity_oauth")
    )

    assert result.status == "ready"


def test_antigravity_token_file_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 154-156: token file exists but is empty -> unreadable error."""
    auth_dir = tmp_path / "gemini_auth"
    cli_dir = auth_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "antigravity-oauth-token").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", str(auth_dir))
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="antigravity", auth_mechanism="antigravity_oauth")
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "unreadable" in res.detail


def test_openrouter_ref_true_reg_def_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 183: has_ref=True but reg_def=None -> unready (missing from SecretRegistry)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    registry = SecretRegistry()
    svc = ProviderDiagnosticsService(secret_registry=registry)
    ctx = _make_context(
        provider="openrouter",
        auth_mechanism="api_key",
        credential_refs=("openrouter_api_key",),
    )
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "missing from SecretRegistry" in res.detail


def test_openrouter_api_key_present_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 205, 225: has_ref=False but env key present -> ready."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-test")
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="openrouter", auth_mechanism="api_key", credential_refs=())
    res = svc.check_credentials(ctx)
    assert res.status == "ready"
    assert not res.blocking


# ---------------------------------------------------------------------------
# Final gap-closing tests
# ---------------------------------------------------------------------------


def test_resolve_codex_auth_path_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 24 (credentials): both CODE_AGENT_CODEX_AUTH_DIR and CODEX_HOME unset -> ~/.codex."""
    from orchestrator.provider_diagnostics_credentials import resolve_codex_auth_path

    monkeypatch.delenv("CODE_AGENT_CODEX_AUTH_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    path = resolve_codex_auth_path()
    assert path.name == "auth.json"
    assert ".codex" in str(path)


def test_resolve_antigravity_token_path_default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 35 (credentials): both env vars unset -> ~/.gemini/.../antigravity-oauth-token."""
    from orchestrator.provider_diagnostics_credentials import resolve_antigravity_token_path

    monkeypatch.delenv("CODE_AGENT_ANTIGRAVITY_AUTH_DIR", raising=False)
    monkeypatch.delenv("GEMINI_HOME", raising=False)
    path = resolve_antigravity_token_path()
    assert path.name == "antigravity-oauth-token"
    assert ".gemini" in str(path)


def test_codex_api_key_no_ref_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 57 (credentials): api_key mode, no credential_refs, no env -> unready."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    svc = ProviderDiagnosticsService()
    # credential_refs empty -> has_ref=False; env unset -> has_env=False
    ctx = _make_context(auth_mechanism="api_key", credential_refs=())
    res = svc.check_credentials(ctx)
    assert res.status == "unready"
    assert "not configured" in res.detail


def test_check_provider_credentials_unknown_provider() -> None:
    """Line 225 (credentials): unknown provider -> ready with 'No provider credential' detail."""
    svc = ProviderDiagnosticsService()
    ctx = _make_context(provider="unknown_provider", auth_mechanism="none")
    res = svc.check_credentials(ctx)
    assert res.status == "ready"
    assert "No provider credential required" in res.detail


def test_resolve_execution_context_runtime_adapter_executable() -> None:
    """Line 68 (provider_diagnostics): executable taken from worker.runtime_adapter.executable."""

    class _Adapter:
        executable = "my-custom-codex"

    class _Worker:
        worker_type = "codex"
        default_runtime_mode = "native_agent"
        runtime_adapter = _Adapter()

    request = WorkerRequest(task_text="Hello")
    ctx = resolve_execution_context(_Worker(), request, task_id="t-exec")
    assert ctx.executable == "my-custom-codex"


@pytest.mark.asyncio
async def test_inspect_image_process_propagates_cancelled_error() -> None:
    """Lines 287-288: CancelledError in _inspect_image_process kills proc and re-raises."""
    import asyncio

    svc = ProviderDiagnosticsService()

    class _FakeProc:
        returncode = None
        kill_called: bool = False
        wait_called: bool = False

        async def communicate(self) -> tuple[bytes, bytes]:
            raise asyncio.CancelledError()

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            return -1

    fake_proc = _FakeProc()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mk:
        mk.return_value = fake_proc
        with pytest.raises(asyncio.CancelledError):
            await svc._inspect_image_process("any:latest")
    assert fake_proc.kill_called
